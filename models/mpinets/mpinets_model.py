import sys
import copy
import kaolin as kl
import numpy as np
import torch
import open3d as o3d
import pytorch_lightning as pl
from torch import nn
from omegaconf import DictConfig
from pytorch_lightning.utilities.types import STEP_OUTPUT
from pointnet2_ops.pointnet2_modules import PointnetSAModule
from typing import Any, List, Tuple, Sequence, Dict, Callable
from enum import Enum, auto 
from datetime import datetime 
from cprint import *
from env.agent.mec_kinova import MecKinova
from env.sampler.mk_sampler import MecKinovaSampler
from models.base import MODEL
from models.mpinets.mpinets_loss import point_clouds_match_loss, sdf_collision_loss
from utils.meckinova_utils import transform_configuration_torch
from utils.transform import transform_pointcloud_torch


@MODEL.register()
class MotionPolicyNetworks(pl.LightningModule):
    """
    The architecture laid out here is the default architecture laid out in the
    Motion Policy Network paper.
    """
    def __init__(self, cfg: DictConfig, *args, **kwargs):
        """
        Constructs the model
        """
        super().__init__()
        self.d_x = cfg.d_x
        self.lr = cfg.lr
        self.collision_loss = cfg.loss.collision_loss
        self.collision_loss_weight = cfg.loss.collision_loss_weight
        self.point_match_loss = cfg.loss.point_match_loss
        self.point_match_ratio = cfg.loss.point_match_ratio
        self.point_match_loss_weight = cfg.loss.point_match_loss_weight

        self.mk_sampler = None
        self.num_agent_points = cfg.scene_model.num_agent_points
        self.train_dataloader_len = None

        self.point_cloud_encoder = MPiNetsPointNet()
        self.feature_encoder = nn.Sequential(
            nn.Linear(self.d_x, 32),
            nn.LeakyReLU(),
            nn.Linear(32, 64),
            nn.LeakyReLU(),
            nn.Linear(64, 128),
            nn.LeakyReLU(),
            nn.Linear(128, 128),
            nn.LeakyReLU(),
            nn.Linear(128, 64),
        )
        self.decoder = nn.Sequential(
            nn.Linear(2048 + 64, 512),
            nn.LeakyReLU(),
            nn.Linear(512, 256),
            nn.LeakyReLU(),
            nn.Linear(256, 128),
            nn.LeakyReLU(),
            nn.Linear(128, self.d_x),
        )

    def configure_optimizers(self):
        """
        A standard method in PyTorch lightning to set the optimizer
        """
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        return optimizer

    def forward(self, xyz: torch.Tensor, q: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
        """
        Passes data through the network to produce an output.

        Arguements:
            xyz {torch.Tensor} -- Tensor representing the point cloud. Should have dimensions of [B x N x 4] 
                                  where B is the batch size, N is the number of points and 4 is because there
                                  are three geometric dimensions and a segmentation mask
            q {torch.Tensor} -- The current robot configuration normalized to be between -1 and 1, according 
                                to each joint's range of motion
        Returns:
            Tuple[torch.Tensor] -- The parameters of GMM to be used to sample the displacement and flg to be 
                                    applied to the current configuration to get the position at the next step 
        """
        pc_encoding = self.point_cloud_encoder(xyz)
        feature_encoding = self.feature_encoder(q)
        z = torch.cat((pc_encoding, feature_encoding), dim=1)
        return self.decoder(z)
    
    def sample(self, q: torch.Tensor) -> torch.Tensor:
        """
        Samples a point cloud from the surface of all the robot's links

        :param q torch.Tensor: Batched configuration in joint space
        :rtype torch.Tensor: Batched point cloud of size [B, self.num_robot_points, 3]
        """
        assert self.mk_sampler is not None
        return self.mk_sampler.sample(q)
    
    def training_step(  # type: ignore[override]
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        A function called automatically by Pytorch Lightning during training. This 
        function handles the forward pass, the loss calculation, and what to log.

        Arguements:
            batch {Dict[str, torch.Tensor]} -- A data batch coming from the data loader, 
                                               should already be on the correct device
            batch_idx {int} -- The index of the batch (not used by this function)
        Returns:
            torch.Tensor -- The overall weighted loss (used for backprop)
        """
        if self.mk_sampler is None:
            self.mk_sampler = MecKinovaSampler(self.device, self.num_agent_points, use_cache=True)

        B = batch['xyz'].shape[0]
        xyz, q, s = batch['xyz'], batch['configuration'], batch['supervision']
        
        ## model output
        q_hat_norm = torch.clamp(self(xyz, q), min=-1, max=1) # [B, D]
        q_hat_unnorm = MecKinova.unnormalize_joints(q_hat_norm) # [B, D]
        s_norm = torch.clamp(s, min=-1, max=1) # [B, D]
        s_unnorm = MecKinova.unnormalize_joints(s_norm) # [B, D]

        ## transform q_hat_unnorm and s_unnorm to agent initial frame
        trans_mats = batch['trans_mat'] # [B, 4, 4]
        trans_mats_inv = torch.inverse(trans_mats) # [B, 4, 4]
        rot_angles = batch['rot_angle'] # [B]
        rot_angles_inv = -rot_angles
        q_hat_unnorm = transform_configuration_torch(q_hat_unnorm, trans_mats_inv, rot_angles_inv)
        s_unnorm = transform_configuration_torch(s_unnorm, trans_mats_inv, rot_angles)

        ## compute loss
        loss = 0
        T_aw = batch['T_aw'] # [B, 4, 4]
        # the normalized scene SDF value in world frame, ranging in [-1, 1]
        sdf_norm_value = batch['sdf_norm_value'] # [B, grid_num, grid_num, grid_num]
        # the scene mesh center in world frame
        scene_mesh_center = batch['scene_mesh_center'] # [B, 3]
        # the scene mesh scale for SDF calculation
        scene_mesh_scale = batch['scene_mesh_scale'] # [B]
        # compute and process agent point clouds
        agent_pcs_a = self.sample(q_hat_unnorm) # [B, N, 3]
        agent_pcs_w = transform_pointcloud_torch(agent_pcs_a, T_aw) # [B, N, 3]
        norm_agent_pcs_w = (agent_pcs_w - scene_mesh_center.unsqueeze(1)) * scene_mesh_scale.unsqueeze(1).unsqueeze(1)

        # compute collision loss
        if self.collision_loss:
            collision_loss = sdf_collision_loss(
                agent_pcs=norm_agent_pcs_w, 
                sdf_norm_values=sdf_norm_value
            )
            self.log('collision_loss', collision_loss)
            loss += self.collision_loss_weight * collision_loss
        
        # compute point match loss
        if self.point_match_loss:
            # NOTE：Since the kinematic chain error of the agent is transmitted from the base link to 
            # the end effector link, when calculating point match loss, the weight calculated by the 
            # link at the end of the agent should be greater.
            predicted_base_pcs = self.mk_sampler.sample_base(q_hat_unnorm)
            predicted_arm_pcs = self.mk_sampler.sample_arm(q_hat_unnorm)
            predicted_gripper_pcs = self.mk_sampler.sample_gripper(q_hat_unnorm)
            target_base_pcs = self.mk_sampler.sample_base(s_unnorm)
            target_arm_pcs = self.mk_sampler.sample_arm(s_unnorm)
            target_gripper_pcs = self.mk_sampler.sample_gripper(s_unnorm)

            point_match_loss = (
                point_clouds_match_loss(predicted_base_pcs, target_base_pcs) * self.point_match_ratio[0] + \
                point_clouds_match_loss(predicted_arm_pcs, target_arm_pcs) * self.point_match_ratio[1] + \
                point_clouds_match_loss(predicted_gripper_pcs, target_gripper_pcs) * self.point_match_ratio[2]
            ) / sum(self.point_match_ratio)
            self.log('point_match_loss', point_match_loss)
            loss += self.point_match_loss_weight * point_match_loss
        
        self.log('val_loss', loss)
        return loss
    
    def validation_step(  # type: ignore[override]
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        This is a Pytorch Lightning function run automatically across devices
        during the validation loop
        """
        pass


class MPiNetsPointNet(pl.LightningModule):
    """
    Point cloud processing networks in mpinets.
    """
    def __init__(self):
        super().__init__()
        self._build_model()
    
    def _build_model(self):
        """
        Assembles the model design into a ModuleList
        """
        self.SA_modules = nn.ModuleList()
        self.SA_modules.append(
            PointnetSAModule(
                npoint=512,
                radius=0.05,
                nsample=128,
                mlp=[1, 64, 64, 64],
                bn=False,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=128,
                radius=0.3,
                nsample=128,
                mlp=[64, 128, 128, 256],
                bn=False,
            )
        )
        self.SA_modules.append(PointnetSAModule(mlp=[256, 512, 512, 1024], bn=False))
        self.fc_layer = nn.Sequential(
            nn.Linear(1024, 4096),
            nn.GroupNorm(16, 4096),
            nn.LeakyReLU(inplace=True),
            nn.Linear(4096, 2048),
            nn.GroupNorm(16, 2048),
            nn.LeakyReLU(inplace=True),
            nn.Linear(2048, 2048),
        )
    
    @staticmethod
    def _break_up_pc(pc: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Breaks up the point cloud into the xyz coordinates and segmentation mask.

        Arguements:
            pc {torch.Tensor} -- Tensor with shape [B, N, M] where M is larger than 3.
                                 The first three dimensions along the last axis will be x, y, z
        Returns:
            Tuple[torch.Tensor, torch.Tensor] -- one with just xyz and one with the corresponding features
        """
        xyz = pc[..., 0:3].contiguous() # Completely copy the tensor
        features = pc[..., 3:].transpose(1, 2).contiguous() # Transpose the tensor
        return xyz, features
    
    def forward(self, point_cloud: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the network.

        Arguements:
            point_cloud {torch.Tensor} -- Has dimensions (B, N, 4)
                                          B is the batch size
                                          N is the number of points
                                          4 is x, y, z, segmentation_mask
                                          This tensor must be on the GPU (CPU tensors not supported)
        Returns:
            torch.Tensor -- The output from the network
        """
        assert point_cloud.size(2) == 4
        xyz, features = self._break_up_pc(point_cloud)

        for module in self.SA_modules:
            xyz = xyz.to(torch.float32)
            features = features.to(torch.float32)
            xyz, features = module(xyz, features)

        return self.fc_layer(features.squeeze(-1))