import torch
import torch.nn as nn
import torch.distributions as distributions
from torch.utils.data import TensorDataset, DataLoader
from torch.utils.tensorboard import SummaryWriter
import gurobipy

import neural_network_lyapunov.relu_system as relu_system


def get_ff_network(dtype, input_dim, output_dim, width, depth,
                   activation=nn.ReLU):
    nn_layers = [nn.Linear(input_dim, width), activation()]
    for i in range(depth):
        nn_layers += [nn.Linear(width, width), activation()]
    nn_layers += [nn.Linear(width, output_dim)]
    model = nn.Sequential(*nn_layers).type(dtype)
    return model


def add_noise(x_data, noise_std_percent):
    """
    @param noise_std tensor with standard deviation of the noise,
    as percent of the mean of the magnitude for that dimension
    """
    assert(isinstance(x_data, torch.Tensor))
    x_data_ = torch.clone(x_data)
    noise_std = noise_std_percent * torch.mean(torch.abs(x_data), dim=0)
    eps = torch.randn(x_data.shape, dtype=x_data.dtype)
    x_data_ += eps * noise_std
    return x_data_


def get_dataloaders(x_data, x_next_data, batch_size, validation_ratio):
    x_dataset = TensorDataset(x_data, x_next_data)
    train_size = int((1. - validation_ratio) * len(x_dataset))
    val_size = len(x_dataset) - train_size
    train_dataset, validation_dataset = torch.utils.data.random_split(
        x_dataset, [train_size, val_size])
    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
    )
    validation_dataloader = DataLoader(
        validation_dataset,
        batch_size=len(validation_dataset),
    )
    return train_dataloader, validation_dataloader


def dataloader_to_rollouts(pbsg, dataloader, dt, N, max_rollouts=None):
    if max_rollouts is None:
        max_rollouts = len(dataloader)
    num_rollouts = 0
    X_rollouts = []
    x_rollouts = []
    for x0, _ in dataloader:
        for i in range(x0.shape[0]):
            rX, rx = pbsg.generate_rollout(x0[i, :], dt, N)
            X_rollouts.append(rX)
            x_rollouts.append(rx)
            num_rollouts += 1
            if num_rollouts >= max_rollouts:
                break
        if num_rollouts >= max_rollouts:
            break
    return X_rollouts, x_rollouts


class DynamicsLearningOptions():
    def __init__(self):
        self.dynynamics_loss_weight = 0.
        self.lyapunov_loss_at_samples_weight = 0.
        self.lyapunov_loss_weight = 0.
        self.equilibrium_loss_weight = 0.

        self.V_lambda = 0.
        self.V_eps = 0.


class DynamicsLearning:
    def __init__(self, train_dataloader, validation_dataloader,
                 relu_system, lyapunov, opt):
        self.train_dataloader = train_dataloader
        self.validation_dataloader = validation_dataloader
        self.relu_system = relu_system
        self.lyapunov = lyapunov

        self.dtype = self.relu_system.dtype
        self.z_dim = self.relu_system.x_dim

        self.z_equilibrium = torch.zeros(self.z_dim, dtype=self.dtype)

        self.V_lambda = opt.V_lambda
        self.V_eps = opt.V_eps

        self.dynynamics_loss_weight = opt.dynynamics_loss_weight
        self.lyapunov_loss_at_samples_weight = \
            opt.lyapunov_loss_at_samples_weight
        self.lyapunov_loss_weight = opt.lyapunov_loss_weight
        self.equilibrium_loss_weight = opt.equilibrium_loss_weight

        self.optimizer = None
        self.writer = None
        self.n_iter = 0

    def lyapunov_loss(self):
        lyap_pos_mip = self.lyapunov.lyapunov_positivity_as_milp(
            self.z_equilibrium, self.V_lambda, self.V_eps)[0]
        lyap_pos_mip.gurobi_model.setParam(gurobipy.GRB.Param.OutputFlag,
                                           False)
        lyap_pos_mip.gurobi_model.optimize()
        lyap_der_mip = self.lyapunov.lyapunov_derivative_as_milp(
            self.z_equilibrium, self.V_lambda, self.V_eps)[0]
        lyap_der_mip.gurobi_model.setParam(gurobipy.GRB.Param.OutputFlag,
                                           False)
        lyap_der_mip.gurobi_model.optimize()
        loss = -lyap_pos_mip.compute_objective_from_mip_data_and_solution() +\
            lyap_der_mip.compute_objective_from_mip_data_and_solution()
        return loss

    def lyapunov_loss_at_samples(self, z):
        z_next = self.relu_system.dynamics_relu(z)
        relu_at_equilibrium = self.lyapunov.lyapunov_relu.forward(
            self.z_equilibrium)
        positivity_sample_loss = \
            self.lyapunov.lyapunov_positivity_loss_at_samples(
                relu_at_equilibrium, self.z_equilibrium,
                z, self.V_lambda, self.V_eps)
        derivative_sample_loss = \
            self.lyapunov.lyapunov_derivative_loss_at_samples_and_next_states(
                self.V_lambda, self.V_eps, z, z_next, self.z_equilibrium)
        loss = positivity_sample_loss + derivative_sample_loss
        return loss

    def equilibrium_loss(self):
        loss = torch.sum(torch.pow(self.relu_system.dynamics_relu(
            self.z_equilibrium) - self.z_equilibrium, 2))
        return loss

    def total_loss(self, x, x_next, validation=False):
        dyn_loss = torch.zeros(1, dtype=self.dtype)
        lyap_loss_samples = torch.zeros(1, dtype=self.dtype)
        lyap_loss = torch.zeros(1, dtype=self.dtype)
        equ_loss = torch.zeros(1, dtype=self.dtype)
        if self.dynynamics_loss_weight > 0:
            dyn_loss = self.dynynamics_loss_weight *\
                self.dynamics_loss(x, x_next)
        if self.lyapunov_loss_at_samples_weight > 0:
            lyap_loss_samples = self.lyapunov_loss_at_samples_weight *\
                self.lyapunov_loss_at_samples(x)
        if not validation:
            if self.lyapunov_loss_weight > 0:
                lyap_loss = self.lyapunov_loss_weight *\
                    self.lyapunov_loss()
            if self.equilibrium_loss_weight > 0:
                equ_loss = self.equilibrium_loss_weight *\
                    self.equilibrium_loss()
        loss = dyn_loss + lyap_loss_samples + lyap_loss + equ_loss
        return loss, dyn_loss, lyap_loss_samples, lyap_loss, equ_loss

    def validation_loss(self):
        with torch.no_grad():
            val_dyn_loss = torch.zeros(1, dtype=self.dtype)
            val_lyapunov_loss_at_samples = torch.zeros(1, dtype=self.dtype)
            for x, x_next in self.validation_dataloader:
                loss, dyn_loss, lyap_loss_samples, lyap_loss, equ_loss = \
                    self.total_loss(x, x_next, validation=True)
                val_dyn_loss += dyn_loss
                val_lyapunov_loss_at_samples += lyap_loss_samples
        return val_dyn_loss, val_lyapunov_loss_at_samples

    def train(self, num_epoch, validate=False):
        if self.optimizer is None:
            params_list = self.get_trainable_parameters()
            params = [{'params': p} for p in params_list]
            self.optimizer = torch.optim.Adam(params)
        if self.writer is None:
            self.writer = SummaryWriter()
            self.n_iter = 0

        for epoch_i in range(num_epoch):
            for x, x_next in self.train_dataloader:

                self.optimizer.zero_grad()
                loss, dyn_loss, lyap_loss_samples, lyap_loss, equ_loss = \
                    self.total_loss(x, x_next)
                loss.backward()
                self.optimizer.step()

                self.n_iter += 1
                self.writer.add_scalar('Loss/train', loss.item(), self.n_iter)
                self.writer.add_scalar('Dynamics/train', dyn_loss.item(),
                                       self.n_iter)
                self.writer.add_scalar('LyapunovSamples/train',
                                       lyap_loss_samples, self.n_iter)
                self.writer.add_scalar('Lyapunov/train', lyap_loss.item(),
                                       self.n_iter)
                self.writer.add_scalar('Equilibrium/train',
                                       equ_loss, self.n_iter)

            if validate:
                val_dyn_loss, val_lyap_loss_samples = self.validation_loss()
                self.writer.add_scalar('Dynamics/validate',
                                       val_dyn_loss.item(), self.n_iter)
                self.writer.add_scalar('LyapunovSamples/validate',
                                       val_lyap_loss_samples, self.n_iter)

    def rollout_validation(self, rollouts):
        assert(isinstance(rollouts, list))
        assert(len(rollouts) >= 1)
        validation_loss = torch.zeros(rollouts[0].shape[0], dtype=self.dtype)
        for r_actual in rollouts:
            validation_loss += self.rollout_loss(r_actual)
        return validation_loss


class LatentSpaceDynamicsLearning(DynamicsLearning):
    def __init__(self, train_dataloader, validation_dataloader,
                 relu_system, lyapunov, opt,
                 encoder, decoder,
                 use_bce=True, use_variational=True):
        super(LatentSpaceDynamicsLearning, self).__init__(
            train_dataloader, validation_dataloader,
            relu_system, lyapunov, opt)
        self.encoder = encoder.type(self.dtype)
        self.decoder = decoder.type(self.dtype)
        self.use_bce = use_bce
        if self.use_bce:
            self.bce_loss = nn.BCELoss(reduction='mean')
            self.bce_loss_none = nn.BCELoss(reduction='none')
        self.use_variational = use_variational
        self.encoder_optimizer = None
        self.encoder_writer = None
        self.encoder_n_iter = 0

    def get_trainable_parameters(self):
        params = [self.relu_system.dynamics_relu.parameters(),
                  self.lyapunov.lyapunov_relu.parameters(),
                  self.encoder.parameters(),
                  self.decoder.parameters()]
        return params

    def reparam(self, z_mu, z_log_var):
        z_std = torch.exp(0.5 * z_log_var)
        eps = torch.randn(z_mu.shape, dtype=z_mu.dtype)
        z = eps * z_std + z_mu
        return z

    def encode_decode(self, x):
        z_mu, z_log_var = self.encoder(x)
        if self.use_variational:
            z = self.reparam(z_mu, z_log_var)
        else:
            z = z_mu
            z_log_var = None
        x_decoded = self.decoder(z)
        return x_decoded, z_mu, z_log_var

    def vae_forward(self, x):
        z_mu, z_log_var = self.encoder(x)
        if self.use_variational:
            z = self.reparam(z_mu, z_log_var)
        else:
            z = z_mu
            z_log_var = None
        x_decoded = self.decoder(z)
        z_next = self.relu_system.dynamics_relu(z)
        x_next_pred_decoded = self.decoder(z_next)
        return x_decoded, x_next_pred_decoded, z_mu, z_log_var

    def kl_loss(self, z_mu, z_log_var):
        loss = torch.mean(-.5 * torch.sum(-torch.pow(z_mu, 2) -
                          torch.exp(z_log_var) + z_log_var + 1., dim=1))
        return loss

    def reconstruction_loss(self, x, x_decoded):
        if self.use_bce:
            loss = self.bce_loss(x_decoded, x)
        else:
            loss = (x - x_decoded).pow(2).mean(dim=[1, 2, 3])[0]
        return loss

    def dynamics_loss(self, x, x_next):
        if x_next.shape[1] < x.shape[1]:
            x_next_ = torch.cat((x[:, x_next.shape[1]:, :, :], x_next), dim=1)
        else:
            x_next_ = x_next
        x_decoded, x_next_pred_decoded, z_mu, z_log_var = self.vae_forward(x)
        loss = self.reconstruction_loss(x, x_decoded)
        loss += self.reconstruction_loss(x_next_, x_next_pred_decoded)
        if self.use_variational:
            loss += self.kl_loss(z_mu, z_log_var)
        return loss

    def encoder_validation_loss(self):
        with torch.no_grad():
            val_loss = torch.zeros(1, dtype=self.dtype)
            for x, x_next in self.validation_dataloader:
                x_decoded, z_mu, z_log_var = self.encode_decode(x)
                loss = self.reconstruction_loss(x, x_decoded)
                if self.use_variational:
                    loss += self.kl_loss(z_mu, z_log_var)
                val_loss += loss
        return val_loss

    def lyapunov_loss_at_samples(self, x):
        z_mu, z_log_var = self.encoder(x)
        if self.use_variational:
            z = self.reparam(z_mu, z_log_var)
        else:
            z = z_mu
        return super(LatentSpaceDynamicsLearning, self).\
            lyapunov_loss_at_samples(z)

    def train_encoder(self, num_epoch, validate=False, device='cpu'):
        if self.encoder_optimizer is None:
            params_list = [self.encoder.parameters(),
                           self.decoder.parameters()]
            params = [{'params': p} for p in params_list]
            self.encoder_optimizer = torch.optim.Adam(params)
        if self.encoder_writer is None:
            self.encoder_writer = SummaryWriter()
            self.encoder_n_iter = 0

        for epoch_i in range(num_epoch):
            for x, x_next in self.train_dataloader:
                self.encoder_optimizer.zero_grad()
                x_decoded, z_mu, z_log_var = self.encode_decode(x)
                loss = self.reconstruction_loss(x, x_decoded)
                if self.use_variational:
                    loss += self.kl_loss(z_mu, z_log_var)
                loss.backward()
                self.encoder_optimizer.step()
                self.encoder_n_iter += 1
                self.encoder_writer.add_scalar('Encoder/train', loss.item(),
                                               self.encoder_n_iter)
            if validate:
                val_loss = self.encoder_validation_loss()
                self.encoder_writer.add_scalar('Encoder/validate',
                                               val_loss.item(),
                                               self.encoder_n_iter)

    def rollout(self, x_init, N, clamp=False):
        assert(len(x_init.shape) == 3)
        x_traj = torch.zeros(N+2, int(x_init.shape[0]/2), x_init.shape[1],
                             x_init.shape[2], dtype=self.dtype)
        x_traj[0, :] = x_init[:int(x_init.shape[0]/2), :, :]
        x_traj[1, :] = x_init[int(x_init.shape[0]/2):, :, :]
        for n in range(N):
            with torch.no_grad():
                x_decoded, x_next_pred_decoded, z_mu, z_log_var = \
                    self.vae_forward(torch.cat((x_traj[n, :], x_traj[n+1, :]),
                                               dim=0).unsqueeze(0))
                if clamp:
                    x_next_pred_decoded = torch.clamp(
                        x_next_pred_decoded, 0, 1)
                x_traj[n+2, :] = x_next_pred_decoded[0,
                                                     int(x_init.shape[0]/2):,
                                                     :, :]
        return x_traj

    def rollout_loss(self, r_actual):
        x0 = torch.cat([r_actual[0, :], r_actual[1, :]], dim=0)
        r_pred = self.rollout(x0, r_actual.shape[0] - 2)
        if self.use_bce:
            loss = self.bce_loss_none(r_actual, r_pred).mean(dim=[1, 2, 3])
        else:
            loss = (r_actual - r_pred).pow(2).mean(dim=[1, 2, 3])
        return loss


class StateSpaceDynamicsLearning(DynamicsLearning):
    def __init__(self, train_dataloader, validation_dataloader,
                 relu_system, lyapunov, opt):
        super(StateSpaceDynamicsLearning, self).__init__(
            train_dataloader, validation_dataloader,
            relu_system, lyapunov, opt)
        self.mse_loss = nn.MSELoss()

    def get_trainable_parameters(self):
        params = [self.relu_system.dynamics_relu.parameters(),
                  self.lyapunov.lyapunov_relu.parameters()]
        return params

    def dynamics_loss(self, x, x_next):
        x_next_pred = self.relu_system.dynamics_relu(x)
        loss = self.mse_loss(x_next_pred, x_next)
        return loss

    def rollout(self, x_init, N):
        assert(len(x_init.shape) == 1)
        assert(x_init.shape[0] == self.z_dim)
        x_traj = torch.zeros(N+1, self.z_dim, dtype=self.dtype)
        x_traj[0, :] = x_init
        for n in range(N):
            with torch.no_grad():
                x_next_pred = self.relu_system.dynamics_relu((x_traj[n:n+1,
                                                             :]))
                x_traj[n+1, :] = x_next_pred[0, :]
        return x_traj

    def rollout_loss(self, r_actual):
        x0 = r_actual[0, :]
        r_pred = self.rollout(x0, r_actual.shape[0] - 1)
        loss = (r_actual - r_pred).pow(2).mean(dim=[1])
        return loss