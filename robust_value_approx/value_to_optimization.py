# -*- coding: utf-8 -*-
import torch
import robust_value_approx.utils as utils
import cvxpy as cp
from pydrake.solvers.mathematicalprogram import MathematicalProgram
from pydrake.solvers.snopt import SnoptSolver
import numpy as np
import jax


class DiffFiniteHorizonValueFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, args):
        """
        Computes the value function for a finite horizon value function
        that is expressed as an MIQP. The derivative is recovered by looking
        at the dual variables at the solution. For now this solves a second
        QP so that cvx returns dual variable. Assuming the following
        problem
        min .5 xᵀ Q1 x + .5 sᵀ Q2 s + .5 αᵀ Q3 α + q1ᵀ x + q2ᵀ s + q3ᵀ α + c
        s.t. Ain1 x + Ain2 s + Ain3 α ≤ rhs_in
             Aeq1 x + Aeq2 s + Aeq3 α = rhs_eq
             α ∈ {0,1}
        we fix both α and x, and we can compute the derivative of the value
        function by computing
        ∂y/∂x = λ₁ᵀAin1 + λ₂ᵀAeq1 + xᵀ Q1 + q1ᵀ

        @param x Tensor representing x0
        @param args dictionnary containing the variables of the problem as
        either cvx variables, parameters or tensor
            -vf (ValueFunction)
            -x0 (Parameter)
            -s (Variable)
            -alpha_mi (Discrete Variable)
            -alpha_con (Variable)
            -obj_mi (Objective)
            -con_mi (list of Constraints)
            -prob_mi (Problem)
            -obj_con (Objective)
            -con_con (list of Constraints)
            -prob_con (Problem)
            -G0 (tensor)
            -A0 (tensor)
            -Q1 (tensor)
            -q1 (tensor)
        """
        ctx.x = x
        x_traj_flat_dim = (1, args['vf'].sys.x_dim*(args['vf'].N-1))
        cost_to_go_dim = args['vf'].N-1
        args['x0'].value = x.detach().numpy()
        args['prob_mi'].solve(
            solver=cp.GUROBI, verbose=False, warm_start=True)
        if args['obj_mi'].value is None:
            ctx.success = False
            return(torch.zeros(x_traj_flat_dim, dtype=x.dtype)*float('nan'),
                   torch.zeros(cost_to_go_dim, dtype=x.dtype)*float('nan'))
        args['alpha_con'].value = args['alpha_mi'].value
        args['prob_con'].solve(
            solver=cp.GUROBI, verbose=False, warm_start=True)
        if args['obj_con'].value is None:
            ctx.success = False
            return(torch.zeros(x_traj_flat_dim, dtype=x.dtype)*float('nan'),
                   torch.zeros(cost_to_go_dim, dtype=x.dtype)*float('nan'))
        assert(abs(args['obj_mi'].value - args['obj_con'].value) <= 1e-5)
        s_tensor = torch.Tensor(args['s'].value).type(x.dtype)
        alpha_mi_tensor = torch.Tensor(args['alpha_mi'].value).type(x.dtype)
        (x_traj_val,
         u_traj_val,
         alpha_traj_val) = args['vf'].sol_to_traj(x, s_tensor, alpha_mi_tensor)
        x_traj_flat = x_traj_val[:, :-1].t().reshape((1, -1))
        step_costs = [args['vf'].step_cost(
            j, x_traj_val[:, j], u_traj_val[:, j],
            alpha_traj_val[:, j]).item() for j in range(args['vf'].N)]
        cost_to_go = torch.Tensor(
            list(np.cumsum(step_costs[::-1]))[::-1]).type(x.dtype)
        ctx.success = True
        ctx.lambda_G = torch.Tensor(
            args['con_con'][0].dual_value).type(x.dtype)
        ctx.lambda_A = torch.Tensor(
            args['con_con'][1].dual_value).type(x.dtype)
        ctx.G0 = args['G0']
        ctx.A0 = args['A0']
        ctx.Q1 = args['Q1']
        ctx.q1 = args['q1']
        return(x_traj_flat, cost_to_go[:-1])

    @staticmethod
    def backward(ctx, grad_output_x_traj_flat, grad_output_cost_to_go):
        # for now only supports one gradient
        assert(torch.all(grad_output_x_traj_flat == 0.))
        assert(torch.all(grad_output_cost_to_go[1:] == 0.))
        if not ctx.success:
            grad_input = torch.zeros(
                ctx.x.shape, dtype=ctx.x.dtype)*float('nan')
            return (grad_input, None)
        dy = ctx.lambda_A.t()@ctx.A0 + ctx.lambda_G.t()@ctx.G0 +\
            ctx.q1 + ctx.Q1@ctx.x
        grad_input = (grad_output_cost_to_go[0] * dy.unsqueeze(0)).squeeze()
        return(grad_input, None)


class ValueFunction:

    def __init__(self, sys, N, x_lo, x_up, u_lo, u_up):
        """
        Class to store the a value function that can be expressed as a
        Mixed-integer quadratic program.

        @param sys: The hybrid linear system used by the value function
        @param N: the number of knot points in the trajectory optimization
        x_lo ≤ x[n] ≤ x_up
        u_lo ≤ u[n] ≤ u_up
        """
        self.sys = sys
        self.dtype = sys.dtype
        self.N = N

        self.x_lo = x_lo.type(self.dtype)
        self.x_up = x_up.type(self.dtype)
        self.u_lo = u_lo.type(self.dtype)
        self.u_up = u_up.type(self.dtype)

        self.Q = None
        self.R = None
        self.Z = None
        self.q = None
        self.r = None
        self.z = None
        self.Qt = None
        self.Rt = None
        self.Zt = None
        self.qt = None
        self.rt = None
        self.zt = None

        self.x0 = None
        self.xN = None

        self.xtraj = None
        self.utraj = None
        self.alphatraj = None

        self.constant_controls = []

    def set_cost(self, Q=None, R=None, Z=None, q=None, r=None, z=None):
        """
        Sets the parameters of the additive cost function (not including
        terminal state)

        ∑(.5 (x[n]-xtraj[n])ᵀ Q (x[n]-xtraj[n]) +
          .5 (u[n]-utraj[n])ᵀ R (u[n]-utraj[n]) +
          .5 (α[n]-αtraj[n])ᵀ Z (α[n]-αtraj[n]) +
          qᵀ(x[n]-xtraj[n]) + rᵀ(u[n]-utraj[n]) + zᵀ(α[n]-αtraj[n]))

        for n = 0...N-1
        """
        if Q is not None:
            self.Q = Q.type(self.dtype)
        if R is not None:
            self.R = R.type(self.dtype)
        if Z is not None:
            self.Z = Z.type(self.dtype)
        if q is not None:
            self.q = q.type(self.dtype)
        if r is not None:
            self.r = r.type(self.dtype)
        if z is not None:
            self.z = z.type(self.dtype)

    def set_terminal_cost(self, Qt=None, Rt=None, Zt=None, qt=None, rt=None,
                          zt=None):
        """
        Set the parameters of the terminal cost

        .5 (x[N]-xtraj[N])ᵀ Qt (x[N]-xtraj[N]) +
        .5 (u[N]-utraj[N])ᵀ Rt (u[N]-utraj[N]) +
        .5 (α[N]-αtraj[N])ᵀ Zt (α[N]-αtraj[N]) +
        qtᵀ(x[N]-xtraj[N]) + rtᵀ(u[N]-utraj[N]) + ztᵀ(α[N]-αtraj[N])
        """
        if Qt is not None:
            self.Qt = Qt.type(self.dtype)
        if Rt is not None:
            self.Rt = Rt.type(self.dtype)
        if Zt is not None:
            self.Zt = Zt.type(self.dtype)
        if qt is not None:
            self.qt = qt.type(self.dtype)
        if rt is not None:
            self.rt = rt.type(self.dtype)
        if zt is not None:
            self.zt = zt.type(self.dtype)

    def set_constraints(self, x0=None, xN=None):
        """
        Sets the constraints for the optimization (imposed on every state
        along the trajectory)

        x[0] == x0
        x[N] == xN
        """
        if x0 is not None:
            self.x0 = x0.type(self.dtype)
        if xN is not None:
            self.xN = xN.type(self.dtype)

    def set_traj(self, xtraj=None, utraj=None, alphatraj=None):
        """
        Sets the desired trajectory (see description of set_cost and
        set_terminal_cost).

        @param xtraj the desired x trajectory as a statedim by N tensor
        @param utraj the desired u trajectory as a inputdim by N tensor
        @param alphatraj the desired x trajectory as a numdiscretestates by
        N tensor
        """
        if xtraj is not None:
            self.xtraj = xtraj.type(self.dtype)
        if utraj is not None:
            self.utraj = utraj.type(self.dtype)
        if alphatraj is not None:
            self.alphatraj = alphatraj.type(self.dtype)

    def set_constant_control(self, control_index):
        """
        Constraint a control input to be constant over a whole trajectory
        @param control_index An integer that is the index of the control
        input that must be kept constant
        """
        self.constant_controls.append(control_index)

    def traj_opt_constraint(self):
        """
        Generates a trajectory optimization problem corresponding to the set
        constraints
        and objectives

        min ∑(.5 (x[n]-xtraj[n])ᵀ Q (x[n]-xtraj[n]) +
              .5 (u[n]-utraj[n])ᵀ R (u[n]-utraj[n]) +
              .5 (α[n]-αtraj[n])ᵀ Z (α[n]-αtraj[n]) +
              qᵀ(x[n]-xtraj[n]) + rᵀ(u[n]-utraj[n]) +
              zᵀ(α[n]-αtraj[n])) +
              .5 (x[N]-xtraj[N])ᵀ Qt (x[N]-xtraj[N]) +
              .5 (u[N]-utraj[N])ᵀ Rt (u[N]-utraj[N]) +
              .5 (α[N]-αtraj[N])ᵀ Zt (α[N]-αtraj[N]) +
              qtᵀ(x[N]-xtraj[N]) + rtᵀ(u[N]-utraj[N]) + ztᵀ(α[N]-αtraj[N])
        x[n+1] = Aᵢ*x[n] + Bᵢ*u[n] + cᵢ
                if Pᵢ * [x[n]; u[n]] <= qᵢ
        x_lo ≤ x[n] ≤ x_up
        u_lo ≤ u[n] ≤ u_up
        x[0] == x0
        x[N] == xN

        the problem is returned in our standard MIQP form so that it can
        easily be passed to verification functions.
        Letting x = x[0], and s = x[1]...x[N]

        min .5 xᵀ Q1 x + .5 sᵀ Q2 s + .5 αᵀ Q3 α + q1ᵀ x + q2ᵀ s + q3ᵀ α + c
        s.t. Ain1 x + Ain2 s + Ain3 α ≤ rhs_in
             Aeq1 x + Aeq2 s + Aeq3 α = rhs_eq
             α ∈ {0,1} (needs to be imposed externally)

        @return Ain1, Ain2, Ain3, rhs_eq, Aeq1, Aeq2, Aeq3, rhs_eq, Q1, Q2, Q3,
        q1, q2, q3, c
        """
        N = self.N
        if self.xtraj is not None:
            assert(self.xtraj.shape[1] == N)
        if self.utraj is not None:
            assert(self.utraj.shape[1] == N)
        if self.alphatraj is not None:
            assert(self.alphatraj.shape[1] == N)
        (Aeq_slack,
         Aeq_alpha,
         Ain_x,
         Ain_u,
         Ain_slack,
         Ain_alpha,
         rhs_in_dyn) = self.sys.mixed_integer_constraints(self.x_lo,
                                                          self.x_up,
                                                          self.u_lo,
                                                          self.u_up)
        xdim = Ain_x.shape[1]
        udim = Ain_u.shape[1]
        slackdim = Ain_slack.shape[1]
        adim = Ain_alpha.shape[1]
        sdim = (xdim + udim + slackdim) * N - xdim
        alphadim = adim * N
        # dynamics inequality constraints
        num_in_dyn = rhs_in_dyn.shape[0]
        s_in_dyn = torch.cat((Ain_x, Ain_u, Ain_slack), 1)
        Ain = torch.zeros(N * num_in_dyn, N *
                          (xdim + udim + slackdim), dtype=self.dtype)
        Ain3 = torch.zeros(N * num_in_dyn, alphadim, dtype=self.dtype)
        rhs_in = torch.zeros(N * num_in_dyn, dtype=self.dtype)
        for i in range(N):
            Ain[i *
                num_in_dyn: (i +
                             1) *
                num_in_dyn, i *
                (xdim +
                 udim +
                 slackdim): (i +
                             1) *
                (xdim +
                 udim +
                 slackdim)] = s_in_dyn
            Ain3[i * num_in_dyn:(i + 1) * num_in_dyn, i *
                 adim:(i + 1) * adim] = Ain_alpha
            rhs_in[i * num_in_dyn:(i + 1) * num_in_dyn] = rhs_in_dyn.squeeze()
        Ain1 = Ain[:, :xdim]
        Ain2 = Ain[:, xdim:]
        # dynamics equality constraints
        num_eq_dyn = xdim
        s_eq_dyn = torch.cat((torch.zeros(num_eq_dyn, xdim +
                                          udim, dtype=self.dtype), Aeq_slack, -
                              torch.eye(xdim, dtype=self.dtype)), 1)
        Aeq = torch.zeros((N - 1) * num_eq_dyn, N *
                          (xdim + udim + slackdim), dtype=self.dtype)
        Aeq3 = torch.zeros((N - 1) * num_eq_dyn, alphadim, dtype=self.dtype)
        rhs_eq = torch.zeros((N - 1) * num_eq_dyn, dtype=self.dtype)
        for i in range(N - 1):
            Aeq[i *
                num_eq_dyn:(i +
                            1) *
                num_eq_dyn, i *
                (xdim +
                 udim +
                 slackdim):(i +
                            1) *
                (xdim +
                 udim +
                 slackdim) +
                xdim] = s_eq_dyn
            Aeq3[i * num_eq_dyn:(i + 1) * num_eq_dyn, i *
                 adim:(i + 1) * adim] = Aeq_alpha
        Aeq1 = Aeq[:, :xdim]
        Aeq2 = Aeq[:, xdim:]
        # one mode at a time
        Aeq1 = torch.cat((Aeq1, torch.zeros(N, xdim, dtype=self.dtype)), 0)
        Aeq2 = torch.cat((Aeq2, torch.zeros(N, sdim, dtype=self.dtype)), 0)
        Aeq_mode = torch.zeros(N, alphadim, dtype=self.dtype)
        for i in range(N):
            Aeq_mode[i, i * adim:(i + 1) * adim] = torch.ones(1,
                                                              adim,
                                                              dtype=self.dtype)
        Aeq3 = torch.cat((Aeq3, Aeq_mode), 0)
        rhs_eq = torch.cat((rhs_eq, torch.ones(N, dtype=self.dtype)), 0)
        # costs
        # slack = [s_1,s_2,...,t_1,t_2,...] where s_i = alpha_i * x_i[n] so the
        # cost on the slack variables is implicit in the costs of alpha and s
        Q1 = torch.zeros(xdim, xdim, dtype=self.dtype)
        q1 = torch.zeros(xdim, dtype=self.dtype)
        Q2 = torch.zeros(sdim, sdim, dtype=self.dtype)
        q2 = torch.zeros(sdim, dtype=self.dtype)
        Q3 = torch.zeros(alphadim, alphadim, dtype=self.dtype)
        q3 = torch.zeros(alphadim, dtype=self.dtype)
        c = 0.
        if self.Q is not None:
            Q1 += self.Q
            if self.xtraj is not None:
                q1 -= self.xtraj[:, 0].T@self.Q
                c += .5 * self.xtraj[:, 0].T@self.Q@self.xtraj[:, 0]
        if self.q is not None:
            q1 += self.q
            if self.xtraj is not None:
                c -= self.q.T@self.xtraj[:, 0]
        if self.R is not None:
            Q2[:udim, :udim] += self.R
            if self.utraj is not None:
                q2[:udim] -= self.utraj[:, 0].T@self.R
                c += .5 * self.utraj[:, 0].T@self.R@self.utraj[:, 0]
        if self.r is not None:
            q2[:udim] += self.r
            if self.utraj is not None:
                c -= self.r.T@self.utraj[:, 0]
        for i in range(N - 2):
            Qi = udim + slackdim + i * (xdim + udim + slackdim)
            Qip = udim + slackdim + i * (xdim + udim + slackdim) + xdim
            Ri = udim + slackdim + i * (xdim + udim + slackdim) + xdim
            Rip = udim + slackdim + i * (xdim + udim + slackdim) + xdim + udim
            if self.Q is not None:
                Q2[Qi:Qip, Qi:Qip] += self.Q
                if self.xtraj is not None:
                    q2[Qi:Qip] -= self.xtraj[:, i + 1].T@self.Q
                    c += .5 *\
                        self.xtraj[:, i + 1].T@self.Q@self.xtraj[:, i + 1]
            if self.R is not None:
                Q2[Ri:Rip, Ri:Rip] += self.R
                if self.utraj is not None:
                    q2[Ri:Rip] -= self.utraj[:, i + 1].T@self.R
                    c += .5 * self.utraj[:, i +
                                         1].T@self.R@self.utraj[:, i + 1]
            if self.q is not None:
                q2[Qi:Qip] += self.q
                if self.xtraj is not None:
                    c -= self.q.T@self.xtraj[:, i + 1]
            if self.r is not None:
                q2[Ri:Rip] += self.r
                if self.utraj is not None:
                    c -= self.r.T@self.utraj[:, i + 1]
        for i in range(N - 1):
            if self.Z is not None:
                Q3[i * adim:(i + 1) * adim, i * adim:(i + 1) * adim] += self.Z
                if self.alphatraj is not None:
                    q3[i * adim:(i + 1) *
                       adim] -= self.alphatraj[:, i].T@self.Z
                    c += .5 * self.alphatraj[:,
                                             i].T@self.Z@self.alphatraj[:, i]
            if self.z is not None:
                q3[i * adim:(i + 1) * adim] += self.z
                if self.alphatraj is not None:
                    c -= self.z.T@self.alphatraj[:, i]
        if self.Qt is not None:
            Q2[-(xdim + udim + slackdim):-(udim + slackdim), -
               (xdim + udim + slackdim):-(udim + slackdim)] += self.Qt
            if self.xtraj is not None:
                q2[-(xdim + udim + slackdim):-(udim + slackdim)
                   ] -= self.xtraj[:, -1].T@self.Qt
                c += .5 * self.xtraj[:, -1].T@self.Qt@self.xtraj[:, -1]
        if self.Rt is not None:
            Q2[-(udim + slackdim):-slackdim, -
               (udim + slackdim):-slackdim] += self.Rt
            if self.utraj is not None:
                q2[-(udim + slackdim):-slackdim] -= self.utraj[:, -1].T@self.Rt
                c += .5 * self.utraj[:, -1].T@self.Rt@self.utraj[:, -1]
        if self.qt is not None:
            q2[-(xdim + udim + slackdim):-(udim + slackdim)] += self.qt
            if self.xtraj is not None:
                c -= self.qt.T@self.xtraj[:, -1]
        if self.rt is not None:
            q2[-(udim + slackdim):-slackdim] += self.rt
            if self.utraj is not None:
                c -= self.rt.T@self.utraj[:, -1]
        if self.Zt is not None:
            Q3[-adim:, -adim:] += self.Zt
            if self.alphatraj is not None:
                q3[-adim:] -= self.alphatraj[:, -1].T@self.Zt
                c += .5 * self.alphatraj[:, -
                                         1].T@self.Zt@self.alphatraj[:, -1]
        if self.zt is not None:
            q3[-adim:] += self.zt
            if self.alphatraj is not None:
                c -= self.zt.T@self.alphatraj[:, -1]
        # state and input constraints
        # x_lo ≤ x[n] ≤ x_up
        # u_lo ≤ u[n] ≤ u_up
        # constraints have to be there otherwise couldn't write dynamics as
        # hybrid linear
        Aup = torch.eye(N * (xdim + udim + slackdim), N *
                        (xdim + udim + slackdim), dtype=self.dtype)
        Aup = Aup[torch.cat(
            (torch.ones(xdim + udim),
                torch.zeros(slackdim))).repeat(N).type(torch.bool), :]
        rhs_up = torch.cat((self.x_up, self.u_up)).repeat(N)
        Ain3 = torch.cat((Ain3, torch.zeros(N * (xdim + udim),
                                            alphadim, dtype=self.dtype)), 0)
        Alo = -torch.eye(N * (xdim + udim + slackdim), N *
                         (xdim + udim + slackdim), dtype=self.dtype)
        Alo = Alo[torch.cat((torch.ones(xdim + udim),
                             torch.zeros(slackdim))).repeat(N).type(
            torch.bool), :]
        rhs_lo = -torch.cat((self.x_lo, self.u_lo)).repeat(N)
        Ain3 = torch.cat((Ain3, torch.zeros(N * (xdim + udim),
                                            alphadim, dtype=self.dtype)), 0)
        Ain1 = torch.cat((Ain1, Aup[:, :xdim], Alo[:, :xdim]), 0)
        Ain2 = torch.cat((Ain2, Aup[:, xdim:], Alo[:, xdim:]), 0)
        rhs_in = torch.cat((rhs_in, rhs_up, rhs_lo))
        # initial state constraints
        # x[0] == x0
        if self.x0 is not None:
            Ax01 = torch.eye(xdim, dtype=self.dtype)
            Ax02 = torch.zeros(xdim, sdim, dtype=self.dtype)
            Ax03 = torch.zeros(xdim, alphadim, dtype=self.dtype)
            Aeq1 = torch.cat((Aeq1, Ax01[~torch.isnan(self.x0), :]), 0)
            Aeq2 = torch.cat((Aeq2, Ax02[~torch.isnan(self.x0), :]), 0)
            Aeq3 = torch.cat((Aeq3, Ax03[~torch.isnan(self.x0), :]), 0)
            rhs_eq = torch.cat((rhs_eq, self.x0[~torch.isnan(self.x0)]))
        # final state constraint
        # x[N] == xN
        if self.xN is not None:
            AxN1 = torch.zeros(xdim, xdim, dtype=self.dtype)
            AxN2 = torch.zeros(xdim, sdim, dtype=self.dtype)
            AxN2[:, -(xdim + udim + slackdim):-(udim + slackdim)
                 ] = torch.eye(xdim, dtype=self.dtype)
            AxN3 = torch.zeros(xdim, alphadim, dtype=self.dtype)
            Aeq1 = torch.cat((Aeq1, AxN1[~torch.isnan(self.xN), :]), 0)
            Aeq2 = torch.cat((Aeq2, AxN2[~torch.isnan(self.xN), :]), 0)
            Aeq3 = torch.cat((Aeq3, AxN3[~torch.isnan(self.xN), :]), 0)
            rhs_eq = torch.cat((rhs_eq, self.xN[~torch.isnan(self.xN)]))
        # constant control input for the whole trajectory
        # u[i,n] == u[i,n+1]
        for constant_control_index in self.constant_controls:
            for i in range(N-1):
                Aeq1 = torch.cat(
                    (Aeq1, torch.zeros(1, xdim, dtype=self.dtype)), 0)
                const_input = torch.zeros(1, sdim, dtype=self.dtype)
                const_input[0, i*(xdim + udim + slackdim) +
                            constant_control_index] = 1.
                const_input[0, (i+1)*(xdim + udim + slackdim) +
                            constant_control_index] = -1.
                Aeq2 = torch.cat((Aeq2, const_input), 0)
                Aeq3 = torch.cat(
                    (Aeq3, torch.zeros(1, alphadim, dtype=self.dtype)), 0)
                rhs_eq = torch.cat(
                    (rhs_eq, torch.zeros(1, dtype=self.dtype)), 0)
        return(Ain1, Ain2, Ain3, rhs_in, Aeq1, Aeq2, Aeq3, rhs_eq,
               Q1, Q2, Q3, q1, q2, q3, c)

    def get_value_function(self):
        """
        return a function that can be evaluated to get the optimal cost-to-go
        for a given initial state. Uses cvxpy in order to solve the cost-to-go

        @return V a function handle that takes x0, the initial state as a
        tensor and returns the associated optimal cost-to-go as a scalar
        """
        traj_opt = self.traj_opt_constraint()
        (Ain1, Ain2, Ain3, rhs_in,
         Aeq1, Aeq2, Aeq3, rhs_eq,
         Q1, Q2, Q3, q1, q2, q3, c) = utils.torch_to_numpy(traj_opt)
        s = cp.Variable(Ain2.shape[1])
        alpha = cp.Variable(Ain3.shape[1], boolean=True)
        x0 = cp.Parameter(Ain1.shape[1])
        obj = cp.Minimize(.5 * cp.quad_form(x0, Q1) +
                          .5 * cp.quad_form(s, Q2) +
                          .5 * cp.quad_form(alpha, Q3) +
                          q1.T@x0 + q2.T@s + q3.T@alpha + c)
        con = [Ain1@x0 + Ain2@s + Ain3@alpha <= rhs_in,
               Aeq1@x0 + Aeq2@s + Aeq3@alpha == rhs_eq]
        prob = cp.Problem(obj, con)

        def V(x):
            if isinstance(x, torch.Tensor):
                x = x.detach().numpy()
            x0.value = x
            prob.solve(solver=cp.GUROBI, verbose=False, warm_start=True)
            if obj.value is not None:
                return(obj.value, torch.Tensor(s.value).type(self.dtype),
                       torch.Tensor(alpha.value).type(self.dtype))
            else:
                return (None, None, None)
        return V

    def sol_to_traj(self, x0, s_val, alpha_val):
        """
        converts a solution to state and input trajectories

        @param x0 Tensor that is the initial state
        @param s_val Tensor solution of s (see traj_opt_constraint)
        @param alpha_val Tensor solution of alpha (see traj_opt_constraint)
        @return x_traj_val Tensor state trajectory
        @return u_traj_val Tesnor input trajectory
        @return alpha_traj_val Tensor discrete state trajectory
        """
        if s_val is None:
            return (None, None, None)
        traj_val = torch.cat((x0, s_val)).reshape(self.N, -1).t()
        x_traj_val = traj_val[:self.sys.x_dim, :]
        u_traj_val = traj_val[
            self.sys.x_dim:self.sys.x_dim+self.sys.u_dim, :]
        alpha_traj_val = alpha_val.reshape(self.N, -1).t()

        return (x_traj_val, u_traj_val, alpha_traj_val)

    def get_q_function(self):
        """
        return a function that can be evaluated to get the optimal cost-to-go
        for a given initial state and initial action. Uses cvxpy in order to
        solve the cost-to-go

        @return Q a function handle that takes x0 and u0, the initial state as
        a tensor and returns the associated optimal cost-to-go as a scalar
        """
        traj_opt = self.traj_opt_constraint()
        (Ain1, Ain2, Ain3, rhs_in,
         Aeq1, Aeq2, Aeq3, rhs_eq,
         Q1, Q2, Q3, q1, q2, q3, c) = utils.torch_to_numpy(traj_opt)
        s = cp.Variable(Ain2.shape[1])
        alpha = cp.Variable(Ain3.shape[1], boolean=True)
        x0 = cp.Parameter(Ain1.shape[1])
        u0 = cp.Parameter(self.sys.u_dim)
        obj = cp.Minimize(.5 * cp.quad_form(x0, Q1) +
                          .5 * cp.quad_form(s, Q2) +
                          .5 * cp.quad_form(alpha, Q3) +
                          q1.T@x0 + q2.T@s + q3.T@alpha + c)
        con = [Ain1@x0 + Ain2@s + Ain3@alpha <= rhs_in,
               Aeq1@x0 + Aeq2@s + Aeq3@alpha == rhs_eq,
               s[:self.sys.u_dim] == u0]
        prob = cp.Problem(obj, con)

        def Q(x, u):
            if isinstance(x, torch.Tensor):
                x = x.detach().numpy()
            if isinstance(u, torch.Tensor):
                u = u.detach().numpy()
            x0.value = x
            u0.value = u
            prob.solve(solver=cp.GUROBI, verbose=False, warm_start=True)
            if obj.value is not None:
                return(obj.value, torch.Tensor(s.value).type(self.dtype),
                       torch.Tensor(alpha.value).type(self.dtype))
            else:
                return (None, None, None)
        return Q

    def get_value_sample_grid(self, x_lo, x_up, num_breaks,
                              update_progress=False):
        """
        generates a uniformly sampled grid of optimal cost-to-go samples
        for this value function

        @param x_lo the lower bound of the sample grid as a tensor
        @param x_up the upper bound of the sample grid as a tensor
        @param num_breaks the number of points along each axis
        as a list of integers (of same dimension as x_lo and x_up)

        @return x_samples a tensor with each row corresponding to an x sample
        @return v_samples a tensor with each row corresponding to the value
        associated with the matching row in x_samples
        """
        assert(len(x_lo) == len(x_up))
        assert(len(x_lo) == len(num_breaks))

        dim_samples = []
        for i in range(len(x_lo)):
            dim_samples.append(torch.linspace(
                x_lo[i], x_up[i], num_breaks[i]).type(self.dtype))
        grid = torch.meshgrid(dim_samples)
        x_samples_all = torch.cat([g.reshape(-1, 1) for g in grid], axis=1)

        x_samples = torch.zeros((0, len(x_lo)), dtype=self.dtype)
        v_samples = torch.zeros((0, 1), dtype=self.dtype)

        V = self.get_value_function()
        for i in range(x_samples_all.shape[0]):
            x = x_samples_all[i, :]
            v = V(x)
            if not isinstance(v[0], type(None)):
                x_samples = torch.cat((x_samples, x.unsqueeze(0)), axis=0)
                v_samples = torch.cat(
                    (v_samples, torch.Tensor([[v[0]]]).type(self.dtype)),
                    axis=0)
            if update_progress:
                utils.update_progress((i + 1) / x_samples_all.shape[0])

        return(x_samples, v_samples)

    def get_rolled_out_sample_grid(self, x_lo, x_up, num_breaks,
                                   num_noisy_samples=0,
                                   noisy_samples_var=1.,
                                   update_progress=False):
        """
        generates a uniformly sampled grid of optimal cost-to-go samples
        for this value function for points between x_lo, x_up.
        Additionally, this function takes the resulting optimal trajectory
        for each sample on the grid, and also computes the optimal
        cost-to-go for these states as well. This generates more samples
        than get_sample_grid, but the samples are heavily biased towards
        the reachable set of the grid

        @param x_lo the lower bound of the sample grid as a tensor
        @param x_up the upper bound of the sample grid as a tensor
        @param num_breaks the number of points along each axis
        as a list of integers (of same dimension as x_lo and x_up)

        @return x_samples a tensor with each row corresponding to an x sample
        @return v_samples a tensor with each row corresponding to the value
        associated with the matching row in x_samples
        """
        assert(len(x_lo) == len(x_up))
        assert(len(x_lo) == len(num_breaks))

        dim_samples = []
        for i in range(len(x_lo)):
            dim_samples.append(torch.linspace(
                x_lo[i], x_up[i], num_breaks[i]).type(self.dtype))
        grid = torch.meshgrid(dim_samples)
        x_samples_all = torch.cat([g.reshape(-1, 1) for g in grid], axis=1)

        x_samples = torch.zeros((0, len(x_lo)), dtype=self.dtype)
        v_samples = torch.zeros((0, 1), dtype=self.dtype)

        V = self.get_value_function()
        for i in range(x_samples_all.shape[0]):
            x = x_samples_all[i, :]
            (obj_val, s_val, alpha_val) = V(x)
            traj_val = torch.cat((x, s_val)).reshape(self.N, -1).t()
            xtraj_val = traj_val[:self.sys.x_dim, :]
            if not isinstance(obj_val, type(None)):
                x_samples = torch.cat((x_samples, x.unsqueeze(0)), axis=0)
                v_samples = torch.cat(
                    (v_samples, torch.Tensor([[obj_val]]).type(self.dtype)),
                    axis=0)
                for j in range(1, self.N):
                    x = xtraj_val[:, j]
                    (obj_val, s_val, alpha_val) = V(x)
                    if not isinstance(obj_val, type(None)):
                        x_samples = torch.cat((x_samples,
                                               x.unsqueeze(0)), axis=0)
                        v_samples = torch.cat((v_samples,
                                               torch.Tensor(
                                                   [[obj_val]]).type(
                                                   self.dtype)), axis=0)
                    else:
                        break
                    for k in range(num_noisy_samples):
                        x = xtraj_val[:, j] + (x_up - x_lo) *\
                            noisy_samples_var *\
                            torch.randn(self.sys.x_dim)
                        (obj_val, s_val, alpha_val) = V(x)
                        if not isinstance(obj_val, type(None)):
                            x_samples = torch.cat((x_samples,
                                                   x.unsqueeze(0)), axis=0)
                            v_samples = torch.cat((v_samples,
                                                   torch.Tensor(
                                                       [[obj_val]]).type(
                                                       self.dtype)),
                                                  axis=0)
            if update_progress:
                utils.update_progress((i + 1) / x_samples_all.shape[0])

        return(x_samples, v_samples)

    def get_q_sample_grid(self, x_lo, x_up, x_num_breaks,
                          u_lo, u_up, u_num_breaks, update_progress=False):
        """
        generates a uniformly sampled grid of Q-values for this value function

        @param x_lo the lower bound of the sample grid of states as a tensor
        @param x_up the upper bound of the sample grid of states as a tensor
        @param x_num_breaks the number of points along each axis
        as a list of integers (of same dimension as x_lo and x_up)
        @param u_lo the lower bound of the sample grid of inputs as a tensor
        @param u_up the upper bound of the sample grid of inputs as a tensor
        @param u_num_breaks see x_num_breaks

        @return x_samples a tensor with each row corresponding to an x sample
        @return u_samples a tensor with each row corresponding to a u sample
        @return v_samples a tensor with each row corresponding to the value
        associated with the matching row in x_samples and u_samples
        """
        assert(len(x_lo) == len(x_up))
        assert(len(x_lo) == len(x_num_breaks))
        assert(len(u_lo) == len(u_up))
        assert(len(u_lo) == len(u_num_breaks))

        dim_samples = []
        for i in range(len(x_lo)):
            dim_samples.append(torch.linspace(
                x_lo[i], x_up[i], x_num_breaks[i]).type(self.dtype))
        for i in range(len(u_lo)):
            dim_samples.append(torch.linspace(
                u_lo[i], u_up[i], u_num_breaks[i]).type(self.dtype))
        grid = torch.meshgrid(dim_samples)
        xu_samples_all = torch.cat([g.reshape(-1, 1) for g in grid], axis=1)

        x_samples = torch.zeros((0, len(x_lo)), dtype=self.dtype)
        u_samples = torch.zeros((0, len(u_lo)), dtype=self.dtype)
        v_samples = torch.zeros((0, 1), dtype=self.dtype)

        Q = self.get_q_function()
        for i in range(xu_samples_all.shape[0]):
            x = xu_samples_all[i, :self.sys.x_dim]
            u = xu_samples_all[i, self.sys.x_dim:]
            v = Q(x, u)
            if not isinstance(v[0], type(None)):
                x_samples = torch.cat((x_samples, x.unsqueeze(0)), axis=0)
                u_samples = torch.cat((u_samples, u.unsqueeze(0)), axis=0)
                v_samples = torch.cat(
                    (v_samples, torch.Tensor([[v[0]]]).type(self.dtype)),
                    axis=0)
            if update_progress:
                utils.update_progress((i + 1) / xu_samples_all.shape[0])

        return(x_samples, u_samples, v_samples)

    def step_cost(self, n, x_val, u_val, alpha_val=None):
        """
        Computes the cost of a time step for this value function.
        @pram n An integer with which time step to evaluate this at
        @param x_val A tensor with the value of the state
        @param u_val A tensor with the value of the control input
        @param alpha_val A tensor with the value of the discrete variables
        """
        assert(n >= 0)
        assert(n <= self.N-1)
        obj = 0.
        if n < self.N-1:
            Q = self.Q
            R = self.R
            Z = self.Z
            q = self.q
            r = self.r
            z = self.z
        else:
            Q = self.Qt
            R = self.Rt
            Z = self.Zt
            q = self.qt
            r = self.rt
            z = self.zt
        if self.xtraj is not None:
            if Q is not None:
                obj += .5 *\
                    (x_val - self.xtraj[:, n])@Q@(x_val - self.xtraj[:, n])
            if q is not None:
                obj += (x_val - self.xtraj[:, n])@q
        else:
            if Q is not None:
                obj += .5 * x_val@Q@x_val
            if q is not None:
                obj += x_val@q
        if self.utraj is not None:
            if R is not None:
                obj += .5 * \
                    (u_val - self.utraj[:, n])@R@(u_val - self.utraj[:, n])
            if r is not None:
                obj += (u_val - self.utraj[:, n])@r
        else:
            if R is not None:
                obj += .5 * u_val@R@u_val
            if r is not None:
                obj += u_val@r
        if alpha_val is not None:
            if self.alphatraj is not None:
                if Z is not None:
                    obj += .5 * \
                        (alpha_val - self.alphatraj[:, n])@Z@\
                        (alpha_val - self.alphatraj[:, n])
                if z is not None:
                    obj += (alpha_val - self.alphatraj[:, n])@z
            else:
                if Z is not None:
                    obj += .5 * alpha_val@Z@alpha_val
                if z is not None:
                    obj += alpha_val@z
        return obj

    def traj_cost(self, x_traj_val, u_traj_val, alpha_traj_val=None):
        """
        Computes the cost of a trajectory for this value function.
        @param x_traj_val A tensor with the value of the state
        @param u_traj_val A tensor with the value of the control input
        @param alpha_traj_val A tensor with the value of the discrete variables
        """
        assert(x_traj_val.shape[1] == self.N)
        assert(u_traj_val.shape[1] == self.N)
        if alpha_traj_val is not None:
            assert(alpha_traj_val.shape[1] == self.N)
        obj = 0.
        for n in range(self.N):
            if alpha_traj_val is not None:
                obj += self.step_cost(n, x_traj_val[:, n], u_traj_val[:, n],
                                      alpha_traj_val[:, n])
            else:
                obj += self.step_cost(n, x_traj_val[:, n], u_traj_val[:, n])
        return obj

    def get_differentiable_value_function(self):
        (G0, G1, G2, h,
         A0, A1, A2, b,
         Q1, Q2, Q3, q1, q2, q3, k) = utils.torch_to_numpy(
            self.traj_opt_constraint())
        G0_tensor = torch.Tensor(G0).type(self.dtype)
        A0_tensor = torch.Tensor(A0).type(self.dtype)
        Q1_tensor = torch.Tensor(Q1).type(self.dtype)
        q1_tensor = torch.Tensor(q1).type(self.dtype)
        x0 = cp.Parameter(G0.shape[1])
        s = cp.Variable(G1.shape[1])
        alpha_mi = cp.Variable(G2.shape[1], boolean=True)
        obj_mi = cp.Minimize(.5 * cp.quad_form(x0, Q1) +
                             .5 * cp.quad_form(s, Q2) +
                             .5 * cp.quad_form(alpha_mi, Q3) +
                             q1.T@x0 + q2.T@s +
                             q3.T@alpha_mi + k)
        con_mi = [G1@s + G2@alpha_mi <= h - G0@x0,
                  A1@s + A2@alpha_mi == b - A0@x0]
        prob_mi = cp.Problem(obj_mi, con_mi)
        alpha_con = cp.Parameter(G2.shape[1], boolean=False)
        obj_con = cp.Minimize(.5 * cp.quad_form(x0, Q1) +
                              .5 * cp.quad_form(s, Q2) +
                              .5 * cp.quad_form(alpha_con, Q3) +
                              q1.T@x0 + q2.T@s +
                              q3.T@alpha_con + k)
        con_con = [G1@s + G2@alpha_con <= h - G0@x0,
                   A1@s + A2@alpha_con == b - A0@x0]
        prob_con = cp.Problem(obj_con, con_con)
        V_args = dict(vf=self,
                      x0=x0,
                      s=s,
                      alpha_mi=alpha_mi,
                      alpha_con=alpha_con,
                      obj_mi=obj_mi,
                      con_mi=con_mi,
                      prob_mi=prob_mi,
                      obj_con=obj_con,
                      con_con=con_con,
                      prob_con=prob_con,
                      G0=G0_tensor,
                      A0=A0_tensor,
                      Q1=Q1_tensor,
                      q1=q1_tensor)
        V_with_grad = lambda x: DiffFiniteHorizonValueFunction.apply(x, V_args)
        return V_with_grad


class DiffFiniteHorizonNLPValueFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, args):
        """
        Computes the value function for a finite horizon value function
        that is expressed as a NLP.
        """
        ctx.x = x
        x_traj_flat_dim = (1, args['vf'].sys.x_dim*(args['vf'].N-1))
        cost_to_go_dim = args['vf'].N-1
        x_np = x.detach().numpy()
        args['vf'].x0_constraint.evaluator().set_bounds(x_np, x_np)
        result = args['vf'].solver.Solve(
            args['vf'].prog, np.zeros(args['vf'].prog.num_vars()), None)
        if not result.is_success():
            ctx.success = False
            return(torch.zeros(x_traj_flat_dim, dtype=x.dtype)*float('nan'),
                   torch.zeros(cost_to_go_dim, dtype=x.dtype)*float('nan'))
        v_val = result.get_optimal_cost()
        s_val = args['vf'].result_to_s(result)
        alpha_val = np.zeros(len(args['vf'].x_traj)) # TODO
        s_tensor = torch.Tensor(s_val).type(x.dtype)
        alpha_mi_tensor = torch.Tensor(alpha_val).type(x.dtype)
        (x_traj_val,
         u_traj_val,
         alpha_traj_val) = args['vf'].sol_to_traj(x, s_tensor, alpha_mi_tensor)
        x_traj_flat = x_traj_val[:, :-1].t().reshape((1, -1))
        step_costs = [args['vf'].step_cost(
            j, x_traj_val[:, j], u_traj_val[:, j],
            alpha_traj_val[:, j]).item() for j in range(args['vf'].N)]
        cost_to_go = torch.Tensor(
            list(np.cumsum(step_costs[::-1]))[::-1]).type(x.dtype)
        grads_g = args['grads_g']
        ddfddx = args['ddfddx']
        dfdx = args['dfdx']
        vf = args['vf']
        det = result.get_solver_details()
        dgdx, ddgddx = grads_g(result)
        Qtl = ddfddx + ddgddx.T@det.Fmul[1:]
        dgnldp = np.zeros((len(det.F)-1, vf.x_dim[vf.mode0]))
        dgbbdp = np.zeros((vf.prog.num_vars(), vf.x_dim[vf.mode0]))
        dgbbdp[:vf.x_dim[vf.mode0],:] = np.eye(vf.x_dim[vf.mode0])
        dgdp = np.concatenate((dgnldp, dgbbdp), axis=0)
        rhs = np.concatenate((np.zeros((vf.prog.num_vars(), vf.x_dim[vf.mode0])), dgdp), axis=0)
        lhs_top = np.concatenate((Qtl, dgdx.T), axis=1)
        lhs_bottom = np.concatenate((dgdx, np.zeros((dgdx.shape[0], dgdx.shape[0]))), axis=1)
        lhs = np.concatenate((lhs_top, lhs_bottom), axis=0)
        penalty = 1e-12*np.eye(lhs.shape[0])
        z = np.linalg.inv(lhs + penalty) @ rhs
        dxdp = z[:vf.prog.num_vars(), :]
        dfdp = dfdx(result)@dxdp
        ctx.dfdp = torch.Tensor(dfdp).type(x.dtype)
        ctx.success = True
        return(x_traj_flat, cost_to_go[:-1])

    @staticmethod
    def backward(ctx, grad_output_x_traj_flat, grad_output_cost_to_go):
        # for now only supports one gradient
        assert(torch.all(grad_output_x_traj_flat == 0.))
        assert(torch.all(grad_output_cost_to_go[1:] == 0.))
        if not ctx.success:
            grad_input = torch.zeros(
                ctx.x.shape, dtype=ctx.x.dtype)*float('nan')
            return (grad_input, None)
        dy = ctx.dfdp
        grad_input = (grad_output_cost_to_go[0] * dy.unsqueeze(0)).squeeze()
        return(grad_input, None)


class NLPValueFunction():
    def __init__(self, sys, x_lo, x_up, u_lo, u_up,
                 dt_lo=0., dt_up=.1, init_mode=0,
                 Q=None, x_desired=None, R=None):
        """
        @param x_lo, x_up, u_lo, u_up are lists of tensors with the lower
        and upper bounds for the states/inputs for each mode
        @param dt_lo, dt_up floats limits for the time step sizes
        @param init_mode integer mode the system initializes in
        """ 
        assert(isinstance(x_lo, list))
        assert(isinstance(x_up, list))
        assert(isinstance(u_lo, list))
        assert(isinstance(u_up, list))
        self.x_lo = x_lo
        self.x_up = x_up
        self.u_lo = u_lo
        self.u_up = u_up
        self.dt_lo = dt_lo
        self.dt_up = dt_up
        self.x_traj = []
        self.u_traj = []
        self.dt_traj = []
        self.mode_traj = []
        self.nl_constraints = []
        self.bb_constraints = []
        self.nl_constraints_dx = []
        self.nl_constraints_ddx = []
        self.x_dim = [len(x) for x in x_lo]
        self.u_dim = [len(u) for u in u_lo]
        for i in range(len(self.x_dim)):
            assert(self.x_dim[i] == self.x_dim[0])
            assert(self.u_dim[i] == self.u_dim[0])
        self.Q = Q
        self.x_desired = x_desired
        self.R = R
        if Q is not None:
            assert(isinstance(Q, list))
        if x_desired is not None:
            assert(isinstance(x_desired, list))
        if R is not None:
            assert(isinstance(R, list))
        self.prog = MathematicalProgram()
        x0, u0, dt0, mode0 = self.add_knot_point(init_mode)
        self.x0 = x0
        self.u0 = u0
        self.dt0 = dt0
        self.mode0 = mode0
        self.solver = SnoptSolver()
        self.sys = sys
        self.x0_constraint = None
        self.Qt = None

    def add_nl_constraint(self, fun, fun_jax, lb=None, ub=None, vars=None):
        con = self.prog.AddConstraint(fun,
            lb=lb, ub=ub,
            vars=vars)
        self.nl_constraints.append(con)
        self.nl_constraints_dx.append(jax.jit(jax.jacfwd(fun_jax)))
        self.nl_constraints_ddx.append(jax.jit(jax.jacfwd(jax.jacrev(fun_jax))))
        return con

    def add_bb_constraint(self, lb=None, ub=None, vars=None):
        con = self.prog.AddBoundingBoxConstraint(lb, ub, vars)
        self.bb_constraints.append(con)
        return con

    def get_last_knot_point(self):
        assert(len(self.x_traj) > 0)
        assert(len(self.u_traj) > 0)
        assert(len(self.dt_traj) > 0)
        assert(len(self.mode_traj) > 0)
        return(self.x_traj[-1], self.u_traj[-1], self.dt_traj[-1],
            self.mode_traj[-1])

    def add_knot_point(self, mode):
        x = self.prog.NewContinuousVariables(
            self.x_dim[mode], "x"+str(len(self.x_traj)))
        u = self.prog.NewContinuousVariables(
            self.u_dim[mode], "u"+str(len(self.u_traj)))
        dt = self.prog.NewContinuousVariables(1, "dt"+str(len(self.dt_traj)))
        self.x_traj.append(x)
        self.u_traj.append(u)
        self.dt_traj.append(dt)
        self.mode_traj.append(mode)
        self.add_bb_constraint(self.x_lo[mode], self.x_up[mode], x)
        self.add_bb_constraint(self.u_lo[mode], self.u_up[mode], u)
        self.add_bb_constraint(self.dt_lo, self.dt_up, dt)
        if self.Q is not None:
            if self.x_desired is not None:
                self.prog.AddQuadraticErrorCost(
                    Q=self.Q[mode], x_desired=self.x_desired[mode], vars=x)
            else:
                self.prog.AddQuadraticCost(
                    Q=self.Q[mode], b=np.zeros(self.x_dim[mode]), c=0., vars=x)
        if self.R is not None:
            self.prog.AddQuadraticCost(
                Q=self.R[mode], b=np.zeros(self.u_dim[mode]), c=0., vars=u)
        return(x, u, dt, mode)

    def add_transition(self, transition_fun, transition_fun_jax,
                       guard, guard_jax, new_mode):
        """
        add a knot point and a mode transition to that knot point
        @param transition_fun function that equals 0 for valid transition
        @param guard function that equals 0 at the transition
        @param new_mode index of the resulting mode
        """
        x0, u0, dt0, mode0 = self.get_last_knot_point()
        x1, u1, dt1, mode1 = self.add_knot_point(new_mode)
        self.add_nl_constraint(transition_fun, transition_fun_jax,
            lb=np.zeros(self.x_dim[mode1]),
            ub=np.zeros(self.x_dim[mode1]),
            vars=np.concatenate([x0, u0, dt0, x1, u1, dt1]))
        self.add_nl_constraint(guard, guard_jax,
            lb=np.array([0.]),
            ub=np.array([0.]),
            vars=np.concatenate([x0, u0, dt0]))

    def add_mode(self, N, dyn_fun, dyn_fun_jax,
                 guard=None, guard_jax=None):
        """
        adds a mode for N knot points
        @param dyn_fun function that equals zero for valid transition
        @param guard function that must be positive for the entire mode
        @param N number of knot points
        """
        for n in range(N):
            x0, u0, dt0, mode0 = self.get_last_knot_point()
            x1, u1, dt1, mode1 = self.add_knot_point(mode0)
            self.add_nl_constraint(dyn_fun, dyn_fun_jax,
                lb=np.zeros(self.x_dim[mode0]),
                ub=np.zeros(self.x_dim[mode0]),
                vars=np.concatenate([x0, u0, dt0, x1, u1, dt1]))
            if guard is not None:
                assert(guard_jax is not None)
                self.add_nl_constraint(guard, guard_jax,
                    lb=np.array([0.]),
                    ub=np.array([np.inf]),
                    vars=np.concatenate([x1, u1, dt1]))

    def add_init_state_constraint(self):
        assert(self.x0_constraint is None)
        self.x0_constraint = self.add_bb_constraint(
            np.zeros(self.x_dim[self.mode0]),
            np.zeros(self.x_dim[self.mode0]),
            self.x0)

    def add_terminal_state_cost(self, Qt):
        assert(self.Qt is None)
        self.Qt = Qt
        self.prog.AddQuadraticErrorCost(Q=Qt, x_desired=self.x_desired[-1],
            vars=self.x_traj[-1])

    def result_to_s(self, result):
        x_traj_sol = [result.GetSolution(x) for x in self.x_traj]
        u_traj_sol = [result.GetSolution(u) for u in self.u_traj]
        s = []
        s.append(u_traj_sol[0])
        for i in range(1, len(x_traj_sol)):
            s.append(x_traj_sol[i])
            s.append(u_traj_sol[i])
        return np.concatenate(s)

    def get_value_function(self):
        assert(self.x0_constraint is not None)
        def V(x):
            assert(isinstance(x, torch.Tensor))
            dtype = x.dtype
            x = x.detach().numpy()
            self.x0_constraint.evaluator().set_bounds(x, x)
            result = self.solver.Solve(
                self.prog, np.zeros(self.prog.num_vars()), None)
            if not result.is_success():
                return(None, None, None)
            v_val = result.get_optimal_cost()
            s_val = self.result_to_s(result)
            alpha_val = np.zeros(len(self.x_traj)) # TODO
            return(v_val, torch.Tensor(s_val).type(dtype),
                   torch.Tensor(alpha_val).type(dtype))
        return V

    def sol_to_traj(self, x0, s_val, alpha_val):
        if s_val is None:
            return (None, None, None)
        assert(isinstance(x0, torch.Tensor))
        assert(isinstance(s_val, torch.Tensor))
        assert(isinstance(alpha_val, torch.Tensor))
        N = len(self.x_traj)
        traj_val = torch.cat((x0, s_val)).reshape(N, -1).t()
        x_traj_val = traj_val[:self.x_dim[0], :]
        u_traj_val = traj_val[
            self.x_dim[0]:self.x_dim[0]+self.u_dim[0], :]
        alpha_traj_val = alpha_val.reshape(N, -1).t()
        return (x_traj_val, u_traj_val, alpha_traj_val)

    def step_cost(self, n, x_val, u_val, alpha_val=None):
        assert(isinstance(x_val, torch.Tensor))
        assert(isinstance(u_val, torch.Tensor))
        if alpha_val is not None:
            assert(isinstance(alpha_val, torch.Tensor))
        mode = self.mode_traj[n]
        cost = 0.
        if self.Q is not None:
            Q = torch.Tensor(self.Q[mode]).type(x_val.dtype)
            if self.x_desired is not None:
                dx = x_val - torch.Tensor(self.x_desired[mode]).type(
                    x_val.dtype)
                cost += dx.t()@Q@dx
            else:
                cost += x_val.t()@Q@x_val
        if self.R is not None:
            R = torch.Tensor(self.R[mode]).type(u_val.dtype)
            cost += u_val.t()@R@u_val
        if self.Qt is not None and n == self.N-1:
            Q = torch.Tensor(self.Qt).type(x_val.dtype)
            if self.x_desired is not None:
                dx = x_val - torch.Tensor(self.x_desired[mode]).type(
                    x_val.dtype)
                cost += dx.t()@Q@dx
            else:
                cost += x_val.t()@Q@x_val
        return cost

    @property
    def N(self):
        return len(self.x_traj)

    @property
    def dtype(self):
        return torch.float64

    def get_differentiable_value_function(self):
        assert(self.x0_constraint is not None)
        ddfddx = np.zeros((self.prog.num_vars(), self.prog.num_vars()))
        for n in range(self.N):
            # x, u, dt
            mode = self.mode_traj[n]
            x_dim = self.x_dim[mode]
            u_dim = self.u_dim[mode]
            x_start = n*(x_dim + u_dim + 1)
            x_end = n*(x_dim + u_dim + 1) + x_dim
            u_start = n*(x_dim + u_dim + 1) + x_dim
            u_end = n*(x_dim + u_dim + 1) + x_dim + u_dim
            if self.Q is not None:
                ddfddx[x_start:x_end, x_start:x_end] = 2.*self.Q[mode]
            if self.R is not None:
                ddfddx[u_start:u_end, u_start:u_end] = 2.*self.R[mode]
            if n == self.N-1 and self.Qt is not None:
                ddfddx[x_start:x_end, x_start:x_end] += 2.*self.Qt    
        def grads_g(result):
            eps_active = 1e-4
            # todo handle transitions, not just dynamics
            J = np.zeros((self.x_dim[0]*len(self.nl_constraints),
                    self.prog.num_vars()))
            H = np.zeros((self.x_dim[0]*len(self.nl_constraints),
                    self.prog.num_vars(), self.prog.num_vars()))
            for n in range(len(self.nl_constraints)):
                x0 = result.GetSolution(self.x_traj[n])
                u0 = result.GetSolution(self.u_traj[n])
                dt0 = result.GetSolution(self.dt_traj[n])
                x1 = result.GetSolution(self.x_traj[n+1])
                u1 = result.GetSolution(self.u_traj[n+1])
                dt1 = result.GetSolution(self.dt_traj[n+1])
                var = np.concatenate([x0, u0, dt0, x1, u1, dt1])
                # todo handle guard constraints too
                j = np.array(self.nl_constraints_dx[n](jax.numpy.array(var)))
                h = np.array(self.nl_constraints_ddx[n](jax.numpy.array(var)))
                J[n*self.x_dim[0]:(n+1)*self.x_dim[0],
                    n*(self.x_dim[0]+self.u_dim[0]+1):(n+2)*(self.x_dim[0]+self.u_dim[0]+1)] = j
                H[n*self.x_dim[0]:(n+1)*self.x_dim[0],
                    n*(self.x_dim[0]+self.u_dim[0]+1):(n+2)*(self.x_dim[0]+self.u_dim[0]+1),
                    n*(self.x_dim[0]+self.u_dim[0]+1):(n+2)*(self.x_dim[0]+self.u_dim[0]+1)] = h
            Jbb = np.zeros((self.prog.num_vars(), self.prog.num_vars()))
            Jstart = 0
            for n in range(self.N):
                mode = self.mode_traj[n]
                x = result.GetSolution(self.x_traj[n])
                u = result.GetSolution(self.u_traj[n])
                dt = result.GetSolution(self.dt_traj[n])
                var = np.concatenate([x, u, dt])
                lb = np.concatenate([self.x_lo[mode], self.u_lo[mode], np.array([self.dt_lo])])
                ub = np.concatenate([self.x_up[mode], self.u_up[mode], np.array([self.dt_up])])
                Jb = np.zeros((len(var), len(var)))
                for i in range(len(var)):
                    if np.abs(var[i] - ub[i]) <= eps_active:
                        Jb[i, i] = 1.
                    elif np.abs(var[i] - lb[i]) <= eps_active:
                        Jb[i, i] = -1.
                if n == 0:
                    Jb[:self.x_dim[mode], :self.x_dim[mode]] = np.eye(self.x_dim[mode])
                Jbb[Jstart:Jstart+len(var),Jstart:Jstart+len(var)] = Jb
                Jstart += len(var)
            return(np.concatenate((J, Jbb), axis=0), H)
        def dfdx(result):
            df = np.zeros(self.prog.num_vars())
            for n in range(self.N):
                # x, u, dt
                mode = self.mode_traj[n]
                x_dim = self.x_dim[mode]
                u_dim = self.u_dim[mode]
                x_start = n*(x_dim + u_dim + 1)
                x_end = n*(x_dim + u_dim + 1) + x_dim
                u_start = n*(x_dim + u_dim + 1) + x_dim
                u_end = n*(x_dim + u_dim + 1) + x_dim + u_dim
                x = result.GetSolution(self.x_traj[n])
                u = result.GetSolution(self.u_traj[n])
                if self.Q is not None:
                    if self.x_desired is not None:
                        df[x_start:x_end] = 2.*self.Q[mode]@\
                            (x - self.x_desired[mode])
                    else:
                        df[x_start:x_end] = 2.*self.Q[mode]@x
                if self.R is not None:
                    df[u_start:u_end] = 2.*self.R[mode]@u
                if n == self.N-1 and self.Qt is not None:
                    if self.x_desired is not None:
                        df[x_start:x_end] += 2.*self.Qt@\
                            (x - self.x_desired[mode])
                    else:
                        df[x_start:x_end] += 2.*self.Qt@x
            return df
        V_args = dict(vf=self, dfdx=dfdx, grads_g=grads_g, ddfddx=ddfddx)
        V_with_grad = lambda x: DiffFiniteHorizonNLPValueFunction.apply(x, V_args)
        return V_with_grad
