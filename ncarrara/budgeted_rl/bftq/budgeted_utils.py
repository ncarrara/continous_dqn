

#!/usr/bin/env python
# -*- coding: utf-8 -*-
from collections import namedtuple

import numpy as np

import matplotlib.pyplot as plt
import os

import torch
import logging

from scipy.spatial.qhull import ConvexHull, QhullError

from ncarrara.utils.os import makedirs
from ncarrara.utils.torch import loss_fonction_factory
from ncarrara.utils_rl.visualization.toolsbox import create_Q_histograms

logger = logging.getLogger(__name__)

OptimalPolicy = namedtuple('OptimalPolicy',
                           ('id_action_inf', 'id_action_sup', 'proba_inf', 'proba_sup', 'budget_inf', 'budget_sup'))

TransitionBudgeted = namedtuple('TransitionBudgeted',
                                ('state', 'action', 'reward', "next_state", 'constraint', 'beta', "hull_id"))


class BudgetedUtils():

    def __init__(self,
                 beta_for_discretization,
                 workspace,
                 device,
                 loss_function,
                 loss_function_c,
                 gamma=0.999,
                 gamma_c=1.0,
                 weights_losses=[1., 1.],
                 id="NO_ID"):
        self.device = device
        self.gamma = gamma
        self.gamma_c = gamma_c
        self.weights_losses = weights_losses
        self.loss_function = loss_fonction_factory(loss_function)
        self.loss_function_c = loss_fonction_factory(loss_function_c)
        self.workspace = workspace
        self.betas_for_discretization = beta_for_discretization
        self.id = id
        raise Exception("Must be reviewed, way less computational efficient than classic BFTQ")

    def reset(self):
        # stateless
        pass

    def change_id(self, id):
        self.id = id

    def loss(self, Q, batch, targets_must_be_zeros=False):

        state_batch = torch.cat(batch.state)
        N_batch = state_batch.shape[0]
        action_batch = torch.cat(batch.action)
        reward_batch = torch.cat(batch.reward)
        constraint_batch = torch.cat(batch.constraint)
        beta_batch = torch.cat(batch.beta)
        state_beta_batch = torch.cat((state_batch, beta_batch.unsqueeze(1)), dim=1)
        next_state_batch = batch.next_state

        nf_mask = torch.tensor([s is not None for s in next_state_batch], device=self.device, dtype=torch.uint8)
        nf_next_state_batch = torch.cat([s for s in next_state_batch if s is not None])
        nf_beta_batch = torch.cat([s for s in batch.beta if s is not None])
        nf_hull_id_batch = torch.cat([s for s in batch.hull_id if s is not None])
        with torch.no_grad():
            next_state_rewards = torch.zeros(N_batch, device=self.device)
            next_state_constraints = torch.zeros(N_batch, device=self.device)
            if not targets_must_be_zeros:
                if torch.sum(nf_mask) > 0:
                    nf_next_state_rewards, nf_next_state_constraints = self._compute_targets(
                        Q, nf_next_state_batch, nf_beta_batch, nf_hull_id_batch)
                    next_state_rewards[nf_mask] = nf_next_state_rewards
                    next_state_constraints[nf_mask] = nf_next_state_constraints
                else:
                    logger.warning("No terminal state")

            expected_state_action_rewards = reward_batch + (self.gamma * next_state_rewards)
            expected_state_action_constraints = constraint_batch + (self.gamma_c * next_state_constraints)
        loss = self._compute_loss(Q, state_beta_batch, action_batch, expected_state_action_rewards,
                                  expected_state_action_constraints)
        return loss

    def _compute_loss(self, Q, state_beta_batch, action_batch, expected_state_action_rewards,
                      expected_state_action_constraints):
        QQ = Q(state_beta_batch)
        u = action_batch.unsqueeze(len(action_batch.shape))
        state_action_rewards = QQ.gather(1, u)
        action_batch_qc = action_batch + Q.predict.out_features / 2
        uc = action_batch_qc.unsqueeze(len(action_batch.shape))
        state_action_constraints = QQ.gather(1, uc)
        loss_Qc = self.loss_function_c(state_action_constraints, expected_state_action_constraints.unsqueeze(1))
        loss_Qr = self.loss_function(state_action_rewards, expected_state_action_rewards.unsqueeze(1))
        w_r, w_c = self.weights_losses
        # if with_weight:
        loss = w_c * loss_Qc + w_r * loss_Qr
        # else:
        #     loss = loss_Qc + loss_Qr

        return loss

    def policy(self, Q, state, beta, action_mask):
        hull = self._compute_hull(s=torch.tensor(state,device=self.device,dtype=torch.float32),
                                  Q=Q,
                                  action_mask=action_mask)
        opt = self._optimal_pia_pib(beta=beta, hull=hull, statistic={})
        rand = np.random.random()
        a = opt.id_action_inf if rand < opt.proba_inf else opt.id_action_sup
        b = opt.budget_inf if rand < opt.proba_inf else opt.budget_sup
        return a, b

    def _compute_targets(self, Q, nf_next_state_batch, nf_beta_batch, nf_hull_id_batch):
        hulls = self._compute_hulls(nf_next_state_batch, nf_hull_id_batch, Q)
        piapib, next_state_beta = self._compute_opts(nf_next_state_batch, nf_beta_batch,
                                                      nf_hull_id_batch, hulls)
        Q_next = Q(next_state_beta)  # self._policy_network
        next_state_rewards, next_state_constraints = self._compute_next_values(Q=Q_next, opts=piapib)
        return next_state_rewards, next_state_constraints

    def _compute_opts(self, nf_next_state_batch, nf_beta_batch, nf_hull_id_batch, hulls):
        N_batch = nf_next_state_batch.shape[0]
        next_state_beta = torch.zeros((N_batch * 2, nf_next_state_batch.shape[-1] + 1), device=self.device)
        i = 0
        opts = [None] * N_batch
        status = {"regular": 0, "not_solvable": 0, "too_much_budget": 0, "exact": 0}
        len_hull = 0
        i_non_terminal = 0
        for next_state, beta, hull_id in zip(nf_next_state_batch, nf_beta_batch, nf_hull_id_batch):
            next_state = next_state.squeeze(0)
            stats = {}
            if i % np.ceil((N_batch / 5)) == 0:
                logger.info("[id={}][compute_opts] processing optimal pia pib {}".format(self.id, i))
            i_non_terminal += 1
            beta = beta.detach().item()
            opts[i] = self._optimal_pia_pib(beta, hulls[i], stats)
            len_hull += len(hulls[i])
            status[stats["status"]] += 1
            budget_inf = torch.tensor(opts[i].budget_inf, device=self.device, dtype=torch.float32).unsqueeze(0)
            budget_sup = torch.tensor(opts[i].budget_sup, device=self.device, dtype=torch.float32).unsqueeze(0)
            x0 = torch.cat((next_state, budget_inf))
            x1 = torch.cat((next_state, budget_sup))
            next_state_beta[i * 2 + 0] = x0.unsqueeze(0)
            next_state_beta[i * 2 + 1] = x1.unsqueeze(0)
            i += 1
        logger.info("[id={}][compute_opts] status : {} for {} transitions. Len(hull)={}"
                    .format(self.id, status, i_non_terminal, len_hull / float(i_non_terminal)))
        return opts, next_state_beta

    def _compute_next_values(self, Q, opts):
        N_actions = Q.shape[-1] // 2
        N_batch = len(opts)
        next_state_rewards = torch.zeros(N_batch, device=self.device)
        next_state_constraints = torch.zeros(N_batch, device=self.device)
        if logger.getEffectiveLevel() is logging.INFO:
            warning_qc_negatif = 0.
            warning_qc__negatif = 0.
            next_state_c_neg = 0.
        for i in range(N_batch):
            if i % np.ceil((N_batch / 5)) == 0:
                logger.info("[id={}] processing mini batch {}".format(self.id, i))
            opt = opts[i]
            qr_ = Q[i * 2][opt.id_action_inf]
            qr = Q[i * 2 + 1][opt.id_action_sup]
            qc_ = Q[i * 2][N_actions + opt.id_action_inf]
            qc = Q[i * 2 + 1][N_actions + opt.id_action_sup]
            next_state_rewards[i] = opt.proba_inf * qr_ + opt.proba_sup * qr
            next_state_constraints[i] = opt.proba_inf * qc_ + opt.proba_sup * qc
            if logger.getEffectiveLevel() is logging.INFO:
                if qc < 0: warning_qc_negatif += 1.
                if qc_ < 0: warning_qc__negatif += 1.
                if next_state_constraints[i] < 0: next_state_c_neg += 1.
            i += 1

        if logger.getEffectiveLevel() is logging.INFO:
            create_Q_histograms("id={}_Qr(s')".format(self.id),
                                values=next_state_rewards.cpu().numpy().flatten(),
                                path=self.workspace / "histogram",
                                labels=["next value"])
            create_Q_histograms("id={}_Qc(s')".format(self.id),
                                values=next_state_constraints.cpu().numpy().flatten(),
                                path=self.workspace / "histogram",
                                labels=["next value"])

            logger.info("[id={}] [WARNING] qc < 0 percentage {:.2f}%"
                        .format(self.id, warning_qc_negatif / N_batch * 100.))
            logger.info("[id={}] [WARNING] qc_ < 0 percentage {:.2f}%"
                        .format(self.id, warning_qc__negatif / N_batch * 100.))
            logger.info("[id={}] [WARNING] next_state_constraints < 0 percentage {:.2f}%"
                        .format(self.id, next_state_c_neg / N_batch * 100.))

        return next_state_rewards, next_state_constraints

    def _optimal_pia_pib(self, beta, hull, statistic):
        if len(hull) == 0:
            raise Exception("Hull is empty")
        elif len(hull) == 1:
            Qc_inf, Qr_inf, beta_inf, action_inf = hull[0]
            res = OptimalPolicy(action_inf, 0, 1., 0., beta_inf, 0.)
            if beta == Qc_inf:
                status = "exact"
            elif beta > Qc_inf:
                status = "too_much_budget"
            else:
                status = "not_solvable"
        else:
            Qc_inf, Qr_inf, beta_inf, action_inf = hull[0]
            if beta < Qc_inf:
                status = "not_solvable"
                res = OptimalPolicy(action_inf, 0, 1., 0., beta_inf, 0.)
            else:
                founded = False
                for k in range(1, len(hull)):
                    Qc_sup, Qr_sup, beta_sup, action_sup = hull[k]
                    if Qc_inf == beta:
                        founded = True
                        status = "exact"  # en realité avec Qc_inf <= beta and beta < Qc_sup ca devrait marcher aussi
                        res = OptimalPolicy(action_inf, 0, 1., 0., beta_inf, 0.)
                        break
                    elif Qc_inf < beta and beta < Qc_sup:
                        founded = True
                        p = (beta - Qc_inf) / (Qc_sup - Qc_inf)
                        status = "regular"
                        res = OptimalPolicy(action_inf, action_sup, 1. - p, p, beta_inf, beta_sup)
                        break
                    else:
                        Qc_inf, Qr_inf, beta_inf, action_inf = Qc_sup, Qr_sup, beta_sup, action_sup
                if not founded:  # we have at least Qc_sup budget
                    status = "too_much_budget"
                    res = OptimalPolicy(action_inf, 0, 1., 0., beta_inf,
                                        0.)  # action_inf = action_sup, beta_inf=beta_sup
        statistic["status"] = status
        return res

    def _compute_hulls(self, states, hull_ids, Q):
        logger.info("[id={}] computing hulls ...".format(self.id))
        action_mask = np.zeros(Q.predict.out_features // 2)
        hulls = np.array([None] * len(states))
        i_computation = 0
        nb_hulls = len(hull_ids)
        computed_hulls = {}
        for i_s, state in enumerate(states):
            hull_id = hull_ids[i_s]
            try:
                hull = computed_hulls[str(hull_id)]
            except KeyError:
                if logger.getEffectiveLevel() is logging.INFO and i_computation % np.ceil(nb_hulls / 5) == 0:
                    logger.info("[id={}] hull computed : {}".format(self.id, i_computation))
                hull = self._compute_hull(s=state, action_mask=action_mask, Q=Q, hull_id=str(hull_id))
                computed_hulls[str(hull_id)] = hull
                i_computation += 1
            hulls[i_s] = hull
        logger.info(("[id={}] hulls actually computed : {}".format(self.id, i_computation)))
        logger.info(("[id={}] total hulls (=next_states) : {}".format(self.id, len(states))))
        logger.info(("[id={}] computing hulls [DONE]".format(self.id)))

        return hulls

    def _compute_hull(self, s, Q, action_mask, hull_id="default"):
        if not type(action_mask) == type(np.zeros(1)):
            action_mask = np.asarray(action_mask)
        # print betas
        N_OK_actions = int(len(action_mask) - np.sum(action_mask))

        dtype = [('Qc', 'f4'), ('Qr', 'f4'), ('beta', 'f4'), ('action', 'i4')]

        workspace = self.workspace / "hulls"
        # print path
        colinearity = False
        test = False
        if logger.getEffectiveLevel() is logging.INFO:
            makedirs(workspace)

        all_points = np.zeros((N_OK_actions * len(self.betas_for_discretization), 2))
        all_betas = np.zeros((N_OK_actions * len(self.betas_for_discretization),))
        all_Qs = np.zeros((N_OK_actions * len(self.betas_for_discretization),), dtype=int)
        max_Qr = -np.inf
        Qc_for_max_Qr = None
        l = 0
        x = np.zeros((N_OK_actions, len(self.betas_for_discretization)))
        y = np.zeros((N_OK_actions, len(self.betas_for_discretization)))
        i_beta = 0
        for beta in self.betas_for_discretization:
            with torch.no_grad():
                b_ = torch.tensor(beta, device=self.device, dtype=torch.float32)
                sb = torch.cat((s.float(), b_.unsqueeze(0)), 0)
                sb = sb.unsqueeze(0)
                QQ = Q(sb)[0]
                QQ = QQ.cpu().detach().numpy()
            for i_a, mask in enumerate(action_mask):
                i_a_ok_act = 0
                if mask == 1:
                    pass
                else:
                    Qr = QQ[i_a]  # TODO c'est peut etre l'inverse ici
                    Qc = QQ[i_a + len(action_mask)]
                    x[i_a_ok_act][i_beta] = Qc
                    y[i_a_ok_act][i_beta] = Qr
                    if Qr > max_Qr:
                        max_Qr = Qr
                        Qc_for_max_Qr = Qc
                    all_points[l] = (Qc, Qr)
                    all_Qs[l] = i_a
                    all_betas[l] = beta
                    l += 1
                    i_a_ok_act += 1
            i_beta += 1

        if logger.getEffectiveLevel() is logging.INFO:
            for i_a in range(0, N_OK_actions):  # len(Q_as)):
                if action_mask[i_a] == 1:
                    pass
                else:
                    plt.plot(x[i_a], y[i_a], linewidth=6, alpha=0.2)
        k = 0
        points = []
        betas = []
        Qs = []
        for point in all_points:
            Qc, Qr = point
            if not (Qr < max_Qr and Qc >= Qc_for_max_Qr):
                # on ajoute que les points non dominés
                points.append(point)
                Qs.append(all_Qs[k])
                betas.append(all_betas[k])
            k += 1
        points = np.array(points)
        if logger.getEffectiveLevel() is logging.INFO:
            # plt.plot(points[:, 0], points[:, 1], '-', linewidth=3, color="tab:cyan")
            plt.plot(all_points[:, 0], all_points[:, 1], 'o', markersize=7, color="blue", alpha=0.1)
            plt.plot(points[:, 0], points[:, 1], 'o', markersize=3, color="red")
            #
            plt.grid()
        try:
            hull = ConvexHull(points)
        except QhullError:
            colinearity = True

        if colinearity:
            idxs_interest_points = range(0, len(points))  # tous les points d'intéret sont bon a prendre
        else:
            stop = False
            max_Qr = -np.inf
            corr_Qc = None
            max_Qr_index = None
            for k in range(0, len(hull.vertices)):
                idx_vertex = hull.vertices[k]
                Qc, Qr = points[idx_vertex]
                if Qr > max_Qr:
                    max_Qr = Qr
                    max_Qr_index = k
                    corr_Qc = Qc
                else:
                    if Qr == max_Qr and Qc < corr_Qc:
                        max_Qr = Qr
                        max_Qr_index = k
                        corr_Qc = Qc
                        k += 1

                    stop = True
                stop = stop or k >= len(hull.vertices)
            idxs_interest_points = []
            stop = False
            # on va itera a l'envers jusq'a ce que Qc change de sens
            Qc_previous = np.inf
            k = max_Qr_index
            j = 0
            # on peut procéder comme ça car c'est counterclockwise
            while not stop:
                idx_vertex = hull.vertices[k]
                Qc, Qr = points[idx_vertex]
                if Qc_previous >= Qc:
                    idxs_interest_points.append(idx_vertex)
                    Qc_previous = Qc
                else:
                    stop = True
                j += 1
                k = (k + 1) % len(hull.vertices)  # counterclockwise
                if j >= len(hull.vertices):
                    stop = True

        if logger.getEffectiveLevel() is logging.INFO:
            plt.title("[id={}] interest_points_colinear={}".format(self.id, colinearity))
            plt.plot(points[idxs_interest_points, 0], points[idxs_interest_points, 1], 'r--', lw=1, color="red")
            plt.plot(points[idxs_interest_points][:, 0], points[idxs_interest_points][:, 1], 'x', markersize=15,
                     color="tab:pink")

            # plt.savefig(workspace + str(hull_id) + ".png", dpi=300)
            plt.savefig("{}/id={}_hull_id={}.png".format(workspace, self.id, hull_id), dpi=300)
            plt.close()

        rez_hull = np.zeros(len(idxs_interest_points), dtype=dtype)
        k = 0
        for idx in idxs_interest_points:
            Qc, Qr = points[idx]
            beta = betas[idx]
            action = Qs[idx]
            rez_hull[k] = np.array([(Qc, Qr, beta, action)], dtype=dtype)
            k += 1
        if colinearity:
            rez_hull = np.sort(rez_hull, order="Qc")
        else:
            rez_hull = np.flip(rez_hull,
                               0)  # normalement si ya pas colinearité c'est deja trié dans l'ordre decroissant
        # print rez
        return rez_hull  # betas, points, idxs_interest_points, Qs, colinearity
