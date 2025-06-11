from typing import Dict, Optional, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
from omegaconf import DictConfig
from cprint import cprint
import pytorch_lightning as pl
from models.base import DIFFUSER
from models.m2diffuser.schedule import make_schedule_ddpm
from models.optimizer.optimizer import Optimizer
from models.planner.planner import Planner

@DIFFUSER.register()
class DDPM(pl.LightningModule):
    def __init__(self, eps_model: nn.Module, cfg: DictConfig, has_obser: bool, *args, **kwargs) -> None:
        super(DDPM, self).__init__()
        
        self.eps_model = eps_model # UNet
        self.timesteps = cfg.timesteps # 200
        self.schedule_cfg = cfg.schedule_cfg # {'beta': [0.0001, 0.01], 'beta_schedule': 'linear', 's': 0.008}
        self.rand_t_type = cfg.rand_t_type # 'half'
        self.converage_opt = cfg.sample.converage.optimization
        self.converage_plan = cfg.sample.converage.planning
        self.converage_ksteps = cfg.sample.converage.ksteps
        self.fine_tune_opt = cfg.sample.fine_tune.optimization
        self.fine_tune_plan = cfg.sample.fine_tune.planning
        self.fine_tune_timesteps = cfg.sample.fine_tune.timesteps
        self.fine_tune_ksteps = cfg.sample.fine_tune.ksteps
        self.lr = cfg.lr
        self.has_observation = has_obser # used in some task giving observation
        self.train_dataloader_len = None

        for k, v in make_schedule_ddpm(self.timesteps, **self.schedule_cfg).items():
            self.register_buffer(k, v)
        
        if cfg.loss_type == 'l1':
            self.criterion = F.l1_loss
        elif cfg.loss_type == 'l2':
            self.criterion = F.mse_loss
        else:
            raise Exception('Unsupported loss type.')
                
        self.optimizer = None
        self.planner = None

    @property
    def device(self):
        return self.betas.device
    
    def apply_observation(self, x_t: torch.Tensor, data: Dict) -> torch.Tensor:
        """ Apply observation to x_t, if self.has_observation if False, this method will return the input

        Args:
            x_t: noisy x in step t
            data: original data provided by dataloader
        """
        ## has start observation, used in path planning and start-conditioned motion generation
        if self.has_observation and 'start' in data:
            start = data['start'] # <B, T, D>
            T = start.shape[1]
            x_t[:, 0:T, :] = start[:, 0:T, :].clone()
        
            if 'obser' in data:
                obser = data['obser']
                O = obser.shape[1]
                x_t[:, T:T+O, :] = obser.clone()
        
        return x_t
    
    def q_sample(self, x0: torch.Tensor, t: torch.Tensor, noise: torch.Tensor) -> torch.Tensor:
        """ Forward difussion process, $q(x_t \mid x_0)$, this process is determinative 
        and has no learnable parameters.

        $x_t = \sqrt{\bar{\alpha}_t} * x0 + \sqrt{1 - \bar{\alpha}_t} * \epsilon$

        Args:
            x0: samples at step 0
            t: diffusion step
            noise: Gaussian noise
        
        Return:
            Diffused samples
        """
        B, *x_shape = x0.shape
        x_t = self.sqrt_alphas_cumprod[t].reshape(B, *((1, ) * len(x_shape))) * x0 + \
            self.sqrt_one_minus_alphas_cumprod[t].reshape(B, *((1, ) * len(x_shape))) * noise

        return x_t

    def forward(self, data: Dict) -> torch.Tensor:
        """ Reverse diffusion process, sampling with the given data containing condition

        Args:
            data: test data, data['x'] gives the target data, data['y'] gives the condition
        
        Return:
            Computed loss
        """
        B = data['x'].shape[0] 

        ## randomly sample timesteps
        if self.rand_t_type == 'all':
            """
            torch.randint:
                low (int, optional) - Lowest integer to be drawn from the distribution. Default: 0.
                high (int) - One above the highest integer to be drawn from the distribution.
                size (tuple) - a tuple defining the shape of the output tensor.
            """
            ts = torch.randint(0, self.timesteps, (B, ), device=self.device).long()
        elif self.rand_t_type == 'half': # √
            ts = torch.randint(0, self.timesteps, ((B + 1) // 2, ), device=self.device)
            if B % 2 == 1:
                ts = torch.cat([ts, self.timesteps - ts[:-1] - 1], dim=0).long()
            else:
                ts = torch.cat([ts, self.timesteps - ts - 1], dim=0).long()
        else:
            raise Exception('Unsupported rand ts type.')
        
        ## generate Gaussian noise
        noise = torch.randn_like(data['x'], device=self.device)

        ## calculate x_t, forward diffusion process
        x_t = self.q_sample(x0=data['x'], t=ts, noise=noise)

        ## apply observation before forwarding to eps model
        ## model need to learn the relationship between the observation frames and the rest frames
        x_t = self.apply_observation(x_t, data)

        ## predict noise
        condtion = self.eps_model.condition(data)
        output = self.eps_model(x_t, ts, condtion)
        ## apply observation after forwarding to eps model
        ## this operation will detach gradient from the loss of the observation tokens
        ## because the target and output of the observation tokens all are constants
        output = self.apply_observation(output, data)

        ## calculate loss
        model_mean, model_variance, model_log_variance = self.p_mean_variance(x_t, ts, condtion)
        if self.optimizer is not None:
            gradient = self.optimizer.gradient(model_mean, data, model_variance)
            output = output + gradient
        if self.planner is not None:
            gradient = self.planner.gradient(model_mean, data, model_variance)
            output = output + gradient

        loss = self.criterion(output, noise)

        return {'loss': loss}
    
    def model_predict(self, x_t: torch.Tensor, t: torch.Tensor, cond: torch.Tensor) -> Tuple:
        """ Get and process model prediction

        $x_0 = \frac{1}{\sqrt{\bar{\alpha}_t}}(x_t - \sqrt{1 - \bar{\alpha}_t}\epsilon_t)$

        Args:
            x_t: denoised sample at timestep t
            t: denoising timestep
            cond: condition tensor
        
        Return:
            The predict target `(pred_noise, pred_x0)`, currently we predict the noise, which is as same as DDPM
        """
        B, *x_shape = x_t.shape

        pred_noise = self.eps_model(x_t, t, cond)
        pred_x0 = self.sqrt_recip_alphas_cumprod[t].reshape(B, *((1, ) * len(x_shape))) * x_t - \
            self.sqrt_recipm1_alphas_cumprod[t].reshape(B, *((1, ) * len(x_shape))) * pred_noise

        return pred_noise, pred_x0
    
    def p_mean_variance(self, x_t: torch.Tensor, t: torch.Tensor, cond: torch.Tensor) -> Tuple:
        """ Calculate the mean and variance, we adopt the following first equation.

        $\tilde{\mu} = \frac{\sqrt{\alpha_t}(1-\bar{\alpha}_{t-1})}{1-\bar{\alpha}_t}x_t + \frac{\sqrt{\bar{\alpha}_{t-1}}\beta_t}{1 - \bar{\alpha}_t}x_0$
        $\tilde{\mu} = \frac{1}{\sqrt{\alpha}_t}(x_t - \frac{1 - \alpha_t}{\sqrt{1 - \bar{\alpha}_t}}\epsilon_t)$

        Args:
            x_t: denoised sample at timestep t
            t: denoising timestep
            cond: condition tensor
        
        Return:
            (model_mean, posterior_variance, posterior_log_variance)
        """
        B, *x_shape = x_t.shape

        ## predict noise and x0 with model $p_\theta$
        pred_noise, pred_x0 = self.model_predict(x_t, t, cond)

        ## calculate mean and variance
        model_mean = self.posterior_mean_coef1[t].reshape(B, *((1, ) * len(x_shape))) * pred_x0 + \
            self.posterior_mean_coef2[t].reshape(B, *((1, ) * len(x_shape))) * x_t
        posterior_variance = self.posterior_variance[t].reshape(B, *((1, ) * len(x_shape)))
        posterior_log_variance = self.posterior_log_variance_clipped[t].reshape(B, *((1, ) * len(x_shape))) # clipped variance

        return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(self, x_t: torch.Tensor, t: int, data: Dict, opt: bool=False, plan: bool=False) -> torch.Tensor:
        """ One step of reverse diffusion process

        $x_{t-1} = \tilde{\mu} + \sqrt{\tilde{\beta}} * z$

        Args:
            x_t: denoised sample at timestep t
            t: denoising timestep
            data: data dict that provides original data and computed conditional feature
            fitting: fit or not

        Return:
            Predict data in the previous step, i.e., $x_{t-1}$
        """
        B, *_ = x_t.shape
        batch_timestep = torch.full((B, ), t, device=self.device, dtype=torch.long)

        if 'cond' in data:
            ## use precomputed conditional feature
            cond = data['cond']
        else:
            ## recompute conditional feature every sampling step
            cond = self.eps_model.condition(data)
        model_mean, model_variance, model_log_variance = self.p_mean_variance(x_t, batch_timestep, cond)
        noise = torch.randn_like(x_t) if t > 0 else 0. # no noise if t == 0

        ## sampling with mean updated by optimizer and planner
        if self.optimizer is not None and opt:
            ## openai guided diffusion uses the input x to compute gradient, see
            ## https://github.com/openai/guided-diffusion/blob/22e0df8183507e13a7813f8d38d51b072ca1e67c/guided_diffusion/gaussian_diffusion.py#L436
            ## But the original formular uses the computed mean?
            gradient = self.optimizer.gradient(model_mean, data, model_variance)
            model_mean = model_mean + gradient
        if self.planner is not None and plan:
            gradient = self.planner.gradient(model_mean, data, model_variance)
            model_mean = model_mean + gradient

        pred_x = model_mean + (0.5 * model_log_variance).exp() * noise

        return pred_x
    
    @torch.no_grad()
    def p_sample_loop(self, data: Dict) -> torch.Tensor:
        """ Reverse diffusion process loop, iteratively sampling

        Args:
            data: test data, data['x'] gives the target data shape
        
        Return:
            Sampled data, <B, T, ...>
        """
        x_t = torch.randn_like(data['x'], device=self.device)
        ## apply observation to x_t
        x_t = self.apply_observation(x_t, data)
        ## precompute conditional feature, which will be used in every sampling step
        condition = self.eps_model.condition(data)
        data['cond'] = condition
        ## iteratively sampling
        all_x_t = [x_t]

        ## converage results
        cprint.info('------------ converage ------------')
        for t in reversed(range(0, self.timesteps)):
            for _ in range(self.converage_ksteps):
                x_t = self.p_sample(x_t, t, data, self.converage_opt, self.converage_plan)
                ## apply observation to x_t
                x_t = self.apply_observation(x_t, data)
                all_x_t.append(x_t)
        ## fine-tuning results
        cprint.info('------------ fine-tune ------------')
        for t in reversed(range(0, self.fine_tune_timesteps)):
            for _ in range(self.fine_tune_ksteps):
                x_t = self.p_sample(x_t, 0, data, self.fine_tune_opt, self.fine_tune_plan)
                ## apply observation to x_t
                x_t = self.apply_observation(x_t, data)
                all_x_t.append(x_t)

        return torch.stack(all_x_t, dim=1)
    
    @torch.no_grad()
    def sample(self, data: Dict, k: int=1) -> torch.Tensor:
        """ Reverse diffusion process, sampling with the given data containing condition
        In this method, the sampled results are unnormalized and converted to absolute representation.

        Args:
            data: test data, data['x'] gives the target data shape
            k: the number of sampled data
        
        Return:
            Sampled results, the shape is <B, k, T, ...>
        """
        ## TODO ddim sample function
        ksamples = []
        for _ in range(k):
            ksamples.append(self.p_sample_loop(data))
        ksamples = torch.stack(ksamples, dim=1)
        
        ## for sequence, normalize and convert repr
        if 'normalizer' in data and data['normalizer'] is not None:
            O = 0
            if self.has_observation and 'start' in data:
                ## the start observation frames are replace during sampling
                _, O, _ = data['start'].shape
            ksamples[..., O:, :] = data['normalizer'].unnormalize(ksamples[..., O:, :])
        if 'repr_type' in data:
            if data['repr_type'] == 'absolute':
                pass
            elif data['repr_type'] == 'relative':
                O = 1
                if self.has_observation and 'start' in data:
                    _, O, _ = data['start'].shape
                ksamples[..., O-1:, :] = torch.cumsum(ksamples[..., O-1:, :], dim=-2)
            else:
                raise Exception('Unsupported repr type.')
        
        return ksamples
    
    def set_optimizer(self, optimizer: Optimizer):
        """ Set optimizer for diffuser, the optimizer is used in sampling

        Args:
            optimizer: a Optimizer object that has a gradient method
        """
        self.optimizer = optimizer
    
    def set_planner(self, planner: Planner):
        """ Set planner for diffuser, the planner is used in sampling

        Args:
            planner: a Planner object that has a gradient method
        """
        self.planner = planner
    
    ## only called by the trainer during the training process
    def configure_optimizers(self):
        """
        A standard method in PyTorch lightning to set the optimizer
        """
        params = []
        nparams = []
        # Returns an iterator over module parameters, yielding both the name 
        # of the parameter as well as the parameter itself
        for n, p in self.named_parameters():
            if p.requires_grad:
                params.append(p)
                nparams.append(p.nelement())
        
        params_group = [
            {'params': params, 'lr': self.lr},
        ]
        optimizer = torch.optim.Adam(params_group) # use adam optimizer in default
        return optimizer
    
    def training_step(  # type: ignore[override]
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        A function called automatically by Pytorch Lightning during training. This 
        function handles the forward pass, the loss calculation, and what to log.
        """
        loss = self(batch)["loss"]
        self.log("val_loss", loss) # Log a key, value pair.
        return loss
    
    def validation_step(  # type: ignore[override]
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        This is a Pytorch Lightning function run automatically across devices
        during the validation loop
        """
        pass
