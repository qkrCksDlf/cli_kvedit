from dataclasses import dataclass

import torch
from torch import Tensor, nn

from flux.modules.layers import (DoubleStreamBlock, EmbedND, LastLayer,
                                 MLPEmbedder, SingleStreamBlock,
                                SingleStreamBlock_kv,DoubleStreamBlock_kv,
                                 timestep_embedding)


@dataclass
class FluxParams:
    in_channels: int
    vec_in_dim: int
    context_in_dim: int
    hidden_size: int
    mlp_ratio: float
    num_heads: int
    depth: int
    depth_single_blocks: int
    axes_dim: list[int]
    theta: int
    qkv_bias: bool
    guidance_embed: bool


class Flux(nn.Module):
    """
    Transformer model for flow matching on sequences.
    """

    def __init__(self, params: FluxParams,double_block_cls=DoubleStreamBlock,single_block_cls=SingleStreamBlock):
        super().__init__()

        self.params = params
        self.in_channels = params.in_channels
        self.out_channels = self.in_channels
        if params.hidden_size % params.num_heads != 0:
            raise ValueError(
                f"Hidden size {params.hidden_size} must be divisible by num_heads {params.num_heads}"
            )
        pe_dim = params.hidden_size // params.num_heads
        if sum(params.axes_dim) != pe_dim:
            raise ValueError(f"Got {params.axes_dim} but expected positional dim {pe_dim}")
        self.hidden_size = params.hidden_size
        self.num_heads = params.num_heads
        self.pe_embedder = EmbedND(dim=pe_dim, theta=params.theta, axes_dim=params.axes_dim)
        self.img_in = nn.Linear(self.in_channels, self.hidden_size, bias=True)
        self.time_in = MLPEmbedder(in_dim=256, hidden_dim=self.hidden_size)
        self.vector_in = MLPEmbedder(params.vec_in_dim, self.hidden_size)
        self.guidance_in = (
            MLPEmbedder(in_dim=256, hidden_dim=self.hidden_size) if params.guidance_embed else nn.Identity()
        )
        self.txt_in = nn.Linear(params.context_in_dim, self.hidden_size)

        self.double_blocks = nn.ModuleList(
            [
                double_block_cls(
                    self.hidden_size,
                    self.num_heads,
                    mlp_ratio=params.mlp_ratio,
                    qkv_bias=params.qkv_bias,
                )
                for _ in range(params.depth)
            ]
        )

        self.single_blocks = nn.ModuleList(
            [
                single_block_cls(self.hidden_size, self.num_heads, mlp_ratio=params.mlp_ratio)
                for _ in range(params.depth_single_blocks)
            ]
        )

        self.final_layer = LastLayer(self.hidden_size, 1, self.out_channels)

    def forward(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor,
        txt_ids: Tensor,
        timesteps: Tensor,
        y: Tensor,
        guidance: Tensor | None = None,
    ) -> Tensor:
        if img.ndim != 3 or txt.ndim != 3:
            raise ValueError("Input img and txt tensors must have 3 dimensions.")

        # running on sequences img
        img = self.img_in(img)
        vec = self.time_in(timestep_embedding(timesteps, 256))
        if self.params.guidance_embed:
            if guidance is None:
                raise ValueError("Didn't get guidance strength for guidance distilled model.")
            vec = vec + self.guidance_in(timestep_embedding(guidance, 256))
        vec = vec + self.vector_in(y)
        txt = self.txt_in(txt)

        ids = torch.cat((txt_ids, img_ids), dim=1)
        pe = self.pe_embedder(ids)

        for block in self.double_blocks:
            img, txt = block(img=img, txt=txt, vec=vec, pe=pe)

        img = torch.cat((txt, img), 1)
        for block in self.single_blocks:
            img = block(img, vec=vec, pe=pe)
        img = img[:, txt.shape[1] :, ...]

        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
        return img

class Flux_kv(Flux):
    """
    继承Flux类，重写forward方法
    """

    def __init__(self, params: FluxParams,double_block_cls=DoubleStreamBlock_kv,single_block_cls=SingleStreamBlock_kv):
        super().__init__(params,double_block_cls,single_block_cls)

    def forward(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor, 
        txt_ids: Tensor,
        timesteps: Tensor,
        y: Tensor, 
        guidance: Tensor | None = None, 
        info: dict = {},
        info_s: dict = {},
        zt_r = None,
        inp_target_s = {}
    ) -> Tensor:
        if img.ndim != 3 or txt.ndim != 3:
            raise ValueError("Input img and txt tensors must have 3 dimensions.")

        # running on sequences img
        img = self.img_in(img)
        
      
        vec = self.time_in(timestep_embedding(timesteps, 256)) 
        if self.params.guidance_embed:
            if guidance is None:
                raise ValueError("Didn't get guidance strength for guidance distilled model.")
            vec = vec + self.guidance_in(timestep_embedding(guidance, 256)) 
        vec = vec + self.vector_in(y)
        txt = self.txt_in(txt)

        ids = torch.cat((txt_ids, img_ids), dim=1) 
        pe = self.pe_embedder(ids) 
        if not info['inverse']:
            zt_r = self.img_in(zt_r) #c추가
            mask_indices = info['mask_indices'] 
            info['pe_mask'] = torch.cat((pe[:, :, :512, ...],pe[:, :, mask_indices+512, ...]),dim=2)

        cnt = 0
        for block in self.double_blocks:
            info['id'] = cnt
            img, txt = block(img=img, txt=txt, vec=vec, pe=pe, info=info, info_s=info_s, zt_r=zt_r, inp_target_s=inp_target_s)
            cnt += 1

        cnt = 0
        x = torch.cat((txt, img), 1) 
        for block in self.single_blocks:
            info['id'] = cnt
            x = block(x, vec=vec, pe=pe, info=info, info_s=info_s, zt_r=zt_r, inp_target_s=inp_target_s)
            cnt += 1

        img = x[:, txt.shape[1] :, ...]

        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)

        return img
