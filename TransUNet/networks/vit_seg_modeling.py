# coding=utf-8
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from typing import Optional, Sequence
import copy
import logging
import math
from pytorch_wavelets import DWTForward

from os.path import join as pjoin

import torch
import torch.nn as nn
from torch.nn import Softmax
import numpy as np
import torch.nn.functional as F

from torch.nn import CrossEntropyLoss, Dropout, Softmax, Linear, Conv2d, LayerNorm
from torch.nn.modules.utils import _pair
from scipy import ndimage
from . import vit_seg_configs as configs
from .vit_seg_modeling_resnet_skip import ResNetV2
from einops.layers.torch import Rearrange



logger = logging.getLogger(__name__)


ATTENTION_Q = "MultiHeadDotProductAttention_1/query/"
ATTENTION_K = "MultiHeadDotProductAttention_1/key/"
ATTENTION_V = "MultiHeadDotProductAttention_1/value/"
ATTENTION_OUT = "MultiHeadDotProductAttention_1/out/"
FC_0 = "MlpBlock_3/Dense_0/"
FC_1 = "MlpBlock_3/Dense_1/"
ATTENTION_NORM = "LayerNorm_0/"
MLP_NORM = "LayerNorm_2/"


def np2th(weights, conv=False):
    """Possibly convert HWIO to OIHW."""
    if conv:
        weights = weights.transpose([3, 2, 0, 1])
    return torch.from_numpy(weights)


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": torch.nn.functional.gelu, "relu": torch.nn.functional.relu, "swish": swish}


class Attention(nn.Module):
    def __init__(self, config, vis):
        super(Attention, self).__init__()
        self.vis = vis
        self.num_attention_heads = config.transformer["num_heads"]
        self.attention_head_size = int(config.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size 

        self.query = Linear(config.hidden_size, self.all_head_size)
        self.key = Linear(config.hidden_size, self.all_head_size)
        self.value = Linear(config.hidden_size, self.all_head_size)

        self.out = Linear(config.hidden_size, config.hidden_size)
        self.attn_dropout = Dropout(config.transformer["attention_dropout_rate"])
        self.proj_dropout = Dropout(config.transformer["attention_dropout_rate"])

        self.softmax = Softmax(dim=-1)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)      # (bs, num_patch, heads, head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states):
        # (8, 196, 768)
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        # (8, 12, 196, 64)
        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # calculations of attention score
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size) 
        attention_probs = self.softmax(attention_scores)

        weights = attention_probs if self.vis else None
        attention_probs = self.attn_dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)  # (bs, heads, patches, head_size)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()  # (bs, patches, heads, head_size)
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,) # (bs, patches, all_head_size)
        context_layer = context_layer.view(*new_context_layer_shape)
        attention_output = self.out(context_layer)
        attention_output = self.proj_dropout(attention_output)
        return attention_output, weights


class Mlp(nn.Module):
    def __init__(self, config):
        super(Mlp, self).__init__()
        self.fc1 = Linear(config.hidden_size, config.transformer["mlp_dim"])
        self.fc2 = Linear(config.transformer["mlp_dim"], config.hidden_size)
        self.act_fn = ACT2FN["gelu"]
        self.dropout = Dropout(config.transformer["dropout_rate"])

        self._init_weights()

    def _init_weights(self):
        nn.init.xavier_uniform_(self.fc1.weight)
        nn.init.xavier_uniform_(self.fc2.weight)
        nn.init.normal_(self.fc1.bias, std=1e-6)
        nn.init.normal_(self.fc2.bias, std=1e-6)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act_fn(x)
        x = self.dropout(x)
        x = self.fc2(x)
        x = self.dropout(x)
        return x


class Embeddings(nn.Module):
    """Construct the embeddings from patch, position embeddings.
    """
    def __init__(self, config, img_size, in_channels=3):
        super(Embeddings, self).__init__()
        self.hybrid = None
        self.config = config
        img_size = _pair(img_size)  # same but pass to a tuple

        if config.patches.get("grid") is not None:   # ResNet
            grid_size = config.patches["grid"]
            patch_size = (img_size[0] // 16 // grid_size[0], img_size[1] // 16 // grid_size[1])
            patch_size_real = (patch_size[0] * 16, patch_size[1] * 16)
            n_patches = (img_size[0] // patch_size_real[0]) * (img_size[1] // patch_size_real[1])  
            self.hybrid = True
        else:
            patch_size = _pair(config.patches["size"])
            n_patches = (img_size[0] // patch_size[0]) * (img_size[1] // patch_size[1])
            self.hybrid = False

        if self.hybrid:
            self.hybrid_model = ResNetV2(block_units=config.resnet.num_layers, width_factor=config.resnet.width_factor)
            in_channels = self.hybrid_model.width * 16
        self.patch_embeddings = Conv2d(in_channels=in_channels,
                                       out_channels=config.hidden_size,
                                       kernel_size=patch_size,
                                       stride=patch_size)
        self.position_embeddings = nn.Parameter(torch.zeros(1, n_patches, config.hidden_size))  # register position embeddings as learnable parameters

        self.dropout = Dropout(config.transformer["dropout_rate"])


    def forward(self, x):
        if self.hybrid:
            x, features = self.hybrid_model(x)
        else:
            features = None
        x = self.patch_embeddings(x)  # (bs, hidden. n_patches^(1/2), n_patches^(1/2))
        x = x.flatten(2)    # (bs, hidden, patches)
        x = x.transpose(-1, -2)  # (bs, n_patches, hidden)

        embeddings = x + self.position_embeddings
        embeddings = self.dropout(embeddings)

        return embeddings, features


# Transformer Layer Block
class Block(nn.Module):
    def __init__(self, config, vis):
        super(Block, self).__init__()
        self.hidden_size = config.hidden_size
        self.attention_norm = LayerNorm(config.hidden_size, eps=1e-6)
        self.ffn_norm = LayerNorm(config.hidden_size, eps=1e-6)
        self.ffn = Mlp(config)
        self.attn = Attention(config, vis)


    def forward(self, x):
        h = x
        x = self.attention_norm(x)
        x, weights = self.attn(x)
        x = x + h

        h = x
        x = self.ffn_norm(x)
        x = self.ffn(x)
        x = x + h
        return x, weights

    def load_from(self, weights, n_block):
        ROOT = f"Transformer/encoderblock_{n_block}/"
        with torch.no_grad():
            query_weight = np2th(weights[pjoin(ROOT, ATTENTION_Q, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            key_weight = np2th(weights[pjoin(ROOT, ATTENTION_K, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            value_weight = np2th(weights[pjoin(ROOT, ATTENTION_V, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            out_weight = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "kernel")]).view(self.hidden_size, self.hidden_size).t()

            query_bias = np2th(weights[pjoin(ROOT, ATTENTION_Q, "bias")]).view(-1)
            key_bias = np2th(weights[pjoin(ROOT, ATTENTION_K, "bias")]).view(-1)
            value_bias = np2th(weights[pjoin(ROOT, ATTENTION_V, "bias")]).view(-1)
            out_bias = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "bias")]).view(-1)

            self.attn.query.weight.copy_(query_weight)
            self.attn.key.weight.copy_(key_weight)
            self.attn.value.weight.copy_(value_weight)
            self.attn.out.weight.copy_(out_weight)
            self.attn.query.bias.copy_(query_bias)
            self.attn.key.bias.copy_(key_bias)
            self.attn.value.bias.copy_(value_bias)
            self.attn.out.bias.copy_(out_bias)

            mlp_weight_0 = np2th(weights[pjoin(ROOT, FC_0, "kernel")]).t()
            mlp_weight_1 = np2th(weights[pjoin(ROOT, FC_1, "kernel")]).t()
            mlp_bias_0 = np2th(weights[pjoin(ROOT, FC_0, "bias")]).t()
            mlp_bias_1 = np2th(weights[pjoin(ROOT, FC_1, "bias")]).t()

            self.ffn.fc1.weight.copy_(mlp_weight_0)
            self.ffn.fc2.weight.copy_(mlp_weight_1)
            self.ffn.fc1.bias.copy_(mlp_bias_0)
            self.ffn.fc2.bias.copy_(mlp_bias_1)

            self.attention_norm.weight.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "scale")]))
            self.attention_norm.bias.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "bias")]))
            self.ffn_norm.weight.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "scale")]))
            self.ffn_norm.bias.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "bias")]))


class Encoder(nn.Module):
    def __init__(self, config, vis):
        super(Encoder, self).__init__()
        self.vis = vis
        self.layer = nn.ModuleList()
        self.encoder_norm = LayerNorm(config.hidden_size, eps=1e-6)
        for _ in range(config.transformer["num_layers"]):
            layer = Block(config, vis)
            self.layer.append(copy.deepcopy(layer))

    def forward(self, hidden_states):
        attn_weights = []
        for layer_block in self.layer:
            hidden_states, weights = layer_block(hidden_states)
            if self.vis:
                attn_weights.append(weights)
        encoded = self.encoder_norm(hidden_states)
        return encoded, attn_weights


class Transformer(nn.Module):
    def __init__(self, config, img_size, vis):
        super(Transformer, self).__init__()
        self.embeddings = Embeddings(config, img_size=img_size)
        self.encoder = Encoder(config, vis)

    def forward(self, input_ids):
        embedding_output, features = self.embeddings(input_ids)
        encoded, attn_weights = self.encoder(embedding_output)  # (B, n_patch, hidden)
        return encoded, attn_weights, features


class Conv2dReLU(nn.Sequential):
    def __init__(
            self,
            in_channels,
            out_channels,
            kernel_size,
            padding=0,
            stride=1,
            use_batchnorm=True,
    ):
        conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size,
            stride=stride,
            padding=padding,
            bias=not (use_batchnorm),
        )
        relu = nn.ReLU(inplace=True)

        bn = nn.BatchNorm2d(out_channels)

        super(Conv2dReLU, self).__init__(conv, bn, relu)


class SpatialAttention(nn.Module):
    def __init__(self):
        super(SpatialAttention, self).__init__()
        self.sa = nn.Conv2d(2, 1, 7, padding=3, padding_mode='reflect', bias=True)

    def forward(self, x):
        x_avg = torch.mean(x, dim=1, keepdim=True)
        x_max, _ = torch.max(x, dim=1, keepdim=True)
        x2 = torch.cat([x_avg, x_max], dim=1)
        sattn = self.sa(x2)
        return sattn


class ChannelAttention(nn.Module):
    def __init__(self, dim, reduction=8):
        super(ChannelAttention, self).__init__()
        self.gap = nn.AdaptiveAvgPool2d(1)
        self.ca = nn.Sequential(
            nn.Conv2d(dim, dim // reduction, 1, padding=0, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(dim // reduction, dim, 1, padding=0, bias=True),
        )

    def forward(self, x):
        x_gap = self.gap(x)
        cattn = self.ca(x_gap)
        return cattn


class tricafe(nn.Module):
    def __init__(self, dim, reduction=8):
        super(tricafe, self).__init__()
        self.sa = SpatialAttention()
        self.ca = ChannelAttention(dim, reduction)
        self.inhb_weight = 0.5

    def forward(self, x, y):
        initial = x + y
        xcy = torch.cat((x, y),dim=1)
        cattn = self.ca(initial)
        sattn = self.sa(initial)
        xcy = self.sa(xcy)

        aux_output1 = cattn + sattn + xcy
        aux_output2 = cattn + sattn
        aux_output3 = cattn + xcy

        aux_output1 = initial + aux_output1 * x + (1 - aux_output1) * y
        aux_output2 = initial + aux_output2 * x + (1 - aux_output2) * y
        aux_output3 = initial + aux_output3 * x + (1 - aux_output3) * y

        result = aux_output1 + aux_output2 + aux_output3
        result = result * self.inhb_weight

        return result

class DecoderBlock(nn.Module):
    def __init__(
            self,
            in_channels,
            out_channels,
            skip_channels=0,
            use_batchnorm=True,
    ):
        super().__init__()
        self.conv1 = Conv2dReLU(
            in_channels + skip_channels,
            out_channels,
            kernel_size=3,
            padding=1,
            use_batchnorm=use_batchnorm,
        )
        self.conv2 = Conv2dReLU(
            out_channels,
            out_channels,
            kernel_size=3,
            padding=1,
            use_batchnorm=use_batchnorm,
        )
        self.up = nn.UpsamplingBilinear2d(scale_factor=2)
        self.tricafe512 = tricafe(512)
        self.tricafe256 = tricafe(256)
        self.sfe_ratio = 3


    def forward(self, x, skip=None):
        x = self.up(x)
        if skip is not None:
            num_channels = x.shape[1]
            if(num_channels == 512):
                skip = self.tricafe512(x, skip)
            if (num_channels == 256):
                skip = self.tricafe256(x, skip)
            x = torch.cat([x, skip*self.sfe_ratio], dim=1)
        x = self.conv1(x)
        x = self.conv2(x)
        return x


class SegmentationHead(nn.Sequential):

    def __init__(self, in_channels, out_channels, kernel_size=3, upsampling=1):
        conv2d = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=kernel_size // 2)
        upsampling = nn.UpsamplingBilinear2d(scale_factor=upsampling) if upsampling > 1 else nn.Identity()
        super().__init__(conv2d, upsampling)

class DecoderCup(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        head_channels = 512
        self.conv_more = Conv2dReLU(
            config.hidden_size,
            head_channels,
            kernel_size=3,
            padding=1,
            use_batchnorm=True,
        )

        decoder_channels = config.decoder_channels
        in_channels = [head_channels] + list(decoder_channels[:-1])
        out_channels = decoder_channels

        if self.config.n_skip != 0:
            skip_channels = self.config.skip_channels
            for i in range(4-self.config.n_skip):
                skip_channels[3-i]=0

        else:
            skip_channels=[0,0,0,0]

        blocks = [
            DecoderBlock(in_ch, out_ch, sk_ch) for in_ch, out_ch, sk_ch in zip(in_channels, out_channels, skip_channels)
        ]
        self.blocks = nn.ModuleList(blocks)

        self.upsample = nn.Upsample(size=(14, 14), mode='bilinear', align_corners=False)

    def forward(self, hidden_states, features=None):
        B, n_patch, hidden = hidden_states.size()
        h, w = int(np.sqrt(n_patch)), int(np.sqrt(n_patch))
        x = hidden_states.permute(0, 2, 1)
        x = x.contiguous().view(B, hidden, h, w)

        x = self.conv_more(x)

        for i, decoder_block in enumerate(self.blocks):
            if features is not None:
                skip = features[i] if (i < self.config.n_skip) else None
            else:
                skip = None
            x = decoder_block(x, skip=skip)
            if i == 0:
                out0 = x
            if i == 1:
                out1 = x
            if i == 2:
                out2 = x

        return x, out0, out1, out2

class d34_Conv2d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(d34_Conv2d, self).__init__()

        self.conv = nn.Conv2d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class d2_Conv2d(nn.Conv2d):

    def forward(self, x):
        w = self.weight
        v, m = torch.var_mean(w, dim=[1, 2, 3], keepdim=True, unbiased=False)
        w = (w - m) / torch.sqrt(v + 1e-5)
        return F.conv2d(x, w, self.bias, self.stride, self.padding,
                        self.dilation, self.groups)


def conv3x3(cin, cout, stride=1, groups=1, bias=False):
    return d2_Conv2d(cin, cout, kernel_size=3, stride=stride,
                     padding=1, bias=bias, groups=groups)


def conv1x1(cin, cout, stride=1, bias=False):
    return d2_Conv2d(cin, cout, kernel_size=1, stride=stride,
                     padding=0, bias=bias)


class PreActBottleneck(nn.Module):
    """Pre-activation (v2) bottleneck block.
    """

    def __init__(self, cin, cout=None, cmid=None, stride=1):
        super().__init__()
        cout = cout or cin
        cmid = cmid or cout // 4

        self.gn1 = nn.GroupNorm(32, cmid, eps=1e-6)
        self.conv1 = conv1x1(cin, cmid, bias=False)
        self.gn2 = nn.GroupNorm(32, cmid, eps=1e-6)
        self.conv2 = conv3x3(cmid, cmid, stride, bias=False)  # Original code has it on conv1!!
        self.gn3 = nn.GroupNorm(32, cout, eps=1e-6)
        self.conv3 = conv1x1(cmid, cout, bias=False)
        self.relu = nn.ReLU(inplace=True)

        if (stride != 1 or cin != cout):
            # Projection also with pre-activation according to paper.
            self.downsample = conv1x1(cin, cout, stride, bias=False)
            self.gn_proj = nn.GroupNorm(cout, cout)

    def forward(self, x):

        # Residual branch
        residual = x
        if hasattr(self, 'downsample'):
            residual = self.downsample(x)
            residual = self.gn_proj(residual)

        # Unit's branch
        y = self.relu(self.gn1(self.conv1(x)))
        y = self.relu(self.gn2(self.conv2(y)))
        y = self.gn3(self.conv3(y))

        y = residual + y

        return y


class upfa(nn.Module):
    def __init__(self, channel):
        super(upfa, self).__init__()
        self.relu = nn.ReLU(True)
        self.channel = channel
        self.reduction = 8

        self.sa = SpatialAttention()
        self.ca = ChannelAttention(channel, self.reduction)

        self.upsample2 = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        self.upsample4 = nn.Upsample(scale_factor=4, mode='bilinear', align_corners=True)

        self.downsample = d34_Conv2d(2 * channel, channel, 3, padding=1)

        self.PreActBTN1_1 = PreActBottleneck(cin=channel, cout=channel * 2, cmid=channel)
        self.PreActBTN1_2 = PreActBottleneck(cin=channel * 2, cout=channel * 2, cmid=channel)
        self.conv256_128 = nn.Conv2d(256, 128, kernel_size=1)

    def forward(self, x1, x2, x3):
        input1 = x1
        input2 = x2

        # Upsampling and fusing features from input1 and input2
        upsampled1 = self.upsample2(input1)
        fused1 = (upsampled1 + input2) * input2
        upsampled_fused1 = self.upsample2(fused1)

        # Processing with bottleneck layers and further upsampling
        preact1 = self.PreActBTN1_1(input1)
        preact2 = self.PreActBTN1_2(preact1)
        upsampled_preact2 = self.upsample4(preact2)
        downsampled_preact2 = self.downsample(upsampled_preact2)

        # Attention mechanisms for fused features
        channel_attn_fused1 = self.ca(x3)
        spatial_attn_fused1 = self.sa(x3)
        attention_combined_fused1 = channel_attn_fused1 + spatial_attn_fused1

        # Fusion with input3 and spatial attention
        combined_with_input3_fused1 = torch.cat((upsampled_fused1, attention_combined_fused1), dim=1)
        combined_with_input3_fused1 = self.conv256_128(combined_with_input3_fused1)
        result = combined_with_input3_fused1 + x3 + downsampled_preact2

        return result

def autopad(kernel_size: int, padding: Optional[int] = None, dilation: int = 1) -> int:
    """Calculate the padding size based on kernel size and dilation."""
    if padding is None:
        padding = (kernel_size - 1) * dilation // 2
    return padding


def make_divisible(value: int, divisor: int = 8) -> int:
    """Make a value divisible by a certain divisor."""
    return int((value + divisor // 2) // divisor * divisor)


class ConvModule(nn.Module):
    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size: int,
            stride: int = 1,
            padding: int = 0,
            dilation: int = 1,
            groups: int = 1,
            norm_cfg: Optional[dict] = None,
            act_cfg: Optional[dict] = None):
        super().__init__()
        layers = []
        layers.append(nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, dilation=dilation, groups=groups, bias=(norm_cfg is None)))
        if norm_cfg:
            norm_layer = self._get_norm_layer(out_channels, norm_cfg)
            layers.append(norm_layer)
        if act_cfg:
            act_layer = self._get_act_layer(act_cfg)
            layers.append(act_layer)
        self.block = nn.Sequential(*layers)

    def forward(self, x):
        return self.block(x)

    def _get_norm_layer(self, num_features, norm_cfg):
        if norm_cfg['type'] == 'BN':
            return nn.BatchNorm2d(num_features, momentum=norm_cfg.get('momentum', 0.1), eps=norm_cfg.get('eps', 1e-5))
        # Add more normalization types if needed
        raise NotImplementedError(f"Normalization layer '{norm_cfg['type']}' is not implemented.")

    def _get_act_layer(self, act_cfg):
        if act_cfg['type'] == 'ReLU':
            return nn.ReLU(inplace=True)
        if act_cfg['type'] == 'SiLU':
            return nn.SiLU(inplace=True)
        # Add more activation types if needed
        raise NotImplementedError(f"Activation layer '{act_cfg['type']}' is not implemented.")

# Update InceptionBottleneck's constructor call to avoid conflicts
# 论文地址：https://arxiv.org/pdf/2403.06258
# 论文：Poly Kernel Inception Network for Remote Sensing Detection(CVPR 2024)
# Github地址：https://github.com/NUST-Machine-Intelligence-Laboratory/PKINet
# Poly Kernel Inception Block(PKIBlock)
class InceptionBottleneck(nn.Module):
    """Bottleneck with Inception module"""
    def __init__(
            self,
            in_channels: int,
            out_channels: Optional[int] = None,
            kernel_sizes: Sequence[int] = (3, 5, 7, 9, 11),
            dilations: Sequence[int] = (1, 1, 1, 1, 1),
            expansion: float = 1.0,
            add_identity: bool = True,
            with_caa: bool = True,
            caa_kernel_size: int = 11,
            norm_cfg: Optional[dict] = dict(type='BN', momentum=0.03, eps=0.001),
            act_cfg: Optional[dict] = dict(type='SiLU')):
        super().__init__()
        out_channels = out_channels or in_channels
        hidden_channels = make_divisible(int(out_channels * expansion), 8)

        self.pre_conv = ConvModule(in_channels, hidden_channels, 1, 1, 0,
                                   norm_cfg=norm_cfg, act_cfg=act_cfg)

        self.dw_conv = ConvModule(hidden_channels, hidden_channels, kernel_sizes[0], 1,
                                  autopad(kernel_sizes[0], None, dilations[0]),
                                  dilation=dilations[0], groups=hidden_channels,
                                  norm_cfg=None, act_cfg=None)
        self.dw_conv1 = ConvModule(hidden_channels, hidden_channels, kernel_sizes[1], 1,
                                   autopad(kernel_sizes[1], None, dilations[1]),
                                   dilation=dilations[1], groups=hidden_channels,
                                   norm_cfg=None, act_cfg=None)
        self.dw_conv2 = ConvModule(hidden_channels, hidden_channels, kernel_sizes[2], 1,
                                   autopad(kernel_sizes[2], None, dilations[2]),
                                   dilation=dilations[2], groups=hidden_channels,
                                   norm_cfg=None, act_cfg=None)
        self.dw_conv3 = ConvModule(hidden_channels, hidden_channels, kernel_sizes[3], 1,
                                   autopad(kernel_sizes[3], None, dilations[3]),
                                   dilation=dilations[3], groups=hidden_channels,
                                   norm_cfg=None, act_cfg=None)
        self.dw_conv4 = ConvModule(hidden_channels, hidden_channels, kernel_sizes[4], 1,
                                   autopad(kernel_sizes[4], None, dilations[4]),
                                   dilation=dilations[4], groups=hidden_channels,
                                   norm_cfg=None, act_cfg=None)
        self.pw_conv = ConvModule(hidden_channels, hidden_channels, 1, 1, 0,
                                  norm_cfg=norm_cfg, act_cfg=act_cfg)

        if with_caa:
            self.caa_factor = CAA(hidden_channels, caa_kernel_size, caa_kernel_size, None, None)
        else:
            self.caa_factor = None

        self.add_identity = add_identity and in_channels == out_channels

        self.post_conv = ConvModule(hidden_channels, out_channels, 1, 1, 0,
                                    norm_cfg=norm_cfg, act_cfg=act_cfg)

    def forward(self, x):
        x = self.pre_conv(x)

        y = x
        x = self.dw_conv(x)
        x = x + self.dw_conv1(x) + self.dw_conv2(x) + self.dw_conv3(x) + self.dw_conv4(x)
        x = self.pw_conv(x)
        if self.caa_factor is not None:
            y = self.caa_factor(y)
        if self.add_identity:
            y = x * y
            x = x + y
        else:
            x = x * y

        x = self.post_conv(x)
        return x

class CAA(nn.Module):
    """Context Anchor Attention"""
    def __init__(
            self,
            channels: int,
            h_kernel_size: int = 11,
            v_kernel_size: int = 11,
            norm_cfg: Optional[dict] = dict(type='BN', momentum=0.03, eps=0.001),
            act_cfg: Optional[dict] = dict(type='SiLU')):
        super().__init__()
        self.avg_pool = nn.AvgPool2d(7, 1, 3)
        self.conv1 = ConvModule(channels, channels, 1, 1, 0,
                                norm_cfg=norm_cfg, act_cfg=act_cfg)
        self.h_conv = ConvModule(channels, channels, (1, h_kernel_size), 1,
                                 (0, h_kernel_size // 2), groups=channels,
                                 norm_cfg=None, act_cfg=None)
        self.v_conv = ConvModule(channels, channels, (v_kernel_size, 1), 1,
                                 (v_kernel_size // 2, 0), groups=channels,
                                 norm_cfg=None, act_cfg=None)
        self.conv2 = ConvModule(channels, channels, 1, 1, 0,
                                norm_cfg=norm_cfg, act_cfg=act_cfg)
        self.act = nn.Sigmoid()

    def forward(self, x):
        attn_factor = self.act(self.conv2(self.v_conv(self.h_conv(self.conv1(self.avg_pool(x))))))
        return attn_factor

def build_act_layer(act_type):
    #Build activation layer
    if act_type is None:
        return nn.Identity()
    assert act_type in ['GELU', 'ReLU', 'SiLU']
    if act_type == 'SiLU':
        return nn.SiLU()
    elif act_type == 'ReLU':
        return nn.ReLU()
    else:
        return nn.GELU()

class ElementScale(nn.Module):
    #A learnable element-wise scaler.

    def __init__(self, embed_dims, init_value=0., requires_grad=True):
        super(ElementScale, self).__init__()
        self.scale = nn.Parameter(
            init_value * torch.ones((1, embed_dims, 1, 1)),
            requires_grad=requires_grad
        )

    def forward(self, x):
        return x * self.scale

# 论文：MogaNet: Multi-order Gated Aggregation Network (ICLR 2024)
# 论文地址：https://arxiv.org/pdf/2211.03295
# Github地址：https://github.com/Westlake-AI/MogaNet
# FFN with Channel Aggregation
class ChannelAggregationFFN(nn.Module):
    """An implementation of FFN with Channel Aggregation.

    Args:
        embed_dims (int): The feature dimension. Same as
            `MultiheadAttention`.
        feedforward_channels (int): The hidden dimension of FFNs.
        kernel_size (int): The depth-wise conv kernel size as the
            depth-wise convolution. Defaults to 3.
        act_type (str): The type of activation. Defaults to 'GELU'.
        ffn_drop (float, optional): Probability of an element to be
            zeroed in FFN. Default 0.0.
    """

    def __init__(self,
                 embed_dims,
                 kernel_size=3,
                 act_type='GELU',
                 ffn_drop=0.):
        super(ChannelAggregationFFN, self).__init__()

        self.embed_dims = embed_dims
        self.feedforward_channels = int(embed_dims * 4)

        self.fc1 = nn.Conv2d(
            in_channels=embed_dims,
            out_channels=self.feedforward_channels,
            kernel_size=1)
        self.dwconv = nn.Conv2d(
            in_channels=self.feedforward_channels,
            out_channels=self.feedforward_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=kernel_size // 2,
            bias=True,
            groups=self.feedforward_channels)
        self.act = build_act_layer(act_type)
        self.fc2 = nn.Conv2d(
            in_channels=self.feedforward_channels,
            out_channels=embed_dims,
            kernel_size=1)
        self.drop = nn.Dropout(ffn_drop)

        self.decompose = nn.Conv2d(
            in_channels=self.feedforward_channels,  # C -> 1
            out_channels=1, kernel_size=1,
        )
        self.sigma = ElementScale(
            self.feedforward_channels, init_value=1e-5, requires_grad=True)
        self.decompose_act = build_act_layer(act_type)

    def feat_decompose(self, x):
        # x_d: [B, C, H, W] -> [B, 1, H, W]
        x = x + self.sigma(x - self.decompose_act(self.decompose(x)))
        return x

    def forward(self, x):
        # proj 1
        x = self.fc1(x)
        x = self.dwconv(x)
        x = self.act(x)
        x = self.drop(x)
        # proj 2
        x = self.feat_decompose(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x



class VisionTransformer(nn.Module):
    def __init__(self, config, img_size=224, num_classes=1, zero_head=False, vis=False):
        super(VisionTransformer, self).__init__()
        self.num_classes = num_classes
        self.zero_head = zero_head
        self.classifier = config.classifier
        self.transformer = Transformer(config, img_size, vis)
        self.decoder = DecoderCup(config)
        self.segmentation_head = SegmentationHead(
            in_channels=config['decoder_channels'][-1],
            out_channels=config['n_classes'],
            kernel_size=3,
        )
        self.config = config
        self.avg_pool = nn.AvgPool2d(kernel_size=2)
        self.segmentation_head0 = SegmentationHead(
            in_channels=256,
            out_channels=config['n_classes'],
            kernel_size=3,
        )
        self.segmentation_head1 = SegmentationHead(
            in_channels=128,
            out_channels=config['n_classes'],
            kernel_size=3,
        )
        self.segmentation_head2 = SegmentationHead(
            in_channels=64,
            out_channels=config['n_classes'],
            kernel_size=3,
        )
        self.conv64_128 = nn.Conv2d(64, 128, kernel_size=1)
        self.conv256_128 = nn.Conv2d(256, 128, kernel_size=1)

        self.upfa128 = upfa(128)
        self.ib = InceptionBottleneck(in_channels=16, out_channels=16)
        self.caffn256 = ChannelAggregationFFN(embed_dims=256)


    def forward(self, x):
        if x.size()[1] == 1:
            x = x.repeat(1,3,1,1)
        x, attn_weights, features = self.transformer(x)  # (B, n_patch, hidden)
        x, out0, out1, out2 = self.decoder(x, features)

        h1 = x      # (8,16,224,224)
        h2 = out0   # (8,256,28,28)
        h3 = out1   # (8,128,56,56)
        h4 = out2   # (8,64,112,112)
        h4_128 = self.conv64_128(h4)
        h2_128 = self.conv256_128(h2)

        h234_1 = self.upfa128(h2_128, h3, h4_128)
        h234_1 *= 2

        h1 = self.ib(h1)
        x = x + h1

        h2 = self.caffn256(h2)
        out0 = out0 + h2

        logits = self.segmentation_head(x)
        out0 = self.segmentation_head0(out0)
        out1 = self.segmentation_head1(out1)
        out2 = self.segmentation_head1(h234_1)

        return logits, out0, out1, out2

    def load_from(self, weights):
        with torch.no_grad():

            res_weight = weights
            self.transformer.embeddings.patch_embeddings.weight.copy_(np2th(weights["embedding/kernel"], conv=True))
            self.transformer.embeddings.patch_embeddings.bias.copy_(np2th(weights["embedding/bias"]))

            self.transformer.encoder.encoder_norm.weight.copy_(np2th(weights["Transformer/encoder_norm/scale"]))
            self.transformer.encoder.encoder_norm.bias.copy_(np2th(weights["Transformer/encoder_norm/bias"]))

            posemb = np2th(weights["Transformer/posembed_input/pos_embedding"])

            posemb_new = self.transformer.embeddings.position_embeddings
            if posemb.size() == posemb_new.size():
                self.transformer.embeddings.position_embeddings.copy_(posemb)
            elif posemb.size()[1]-1 == posemb_new.size()[1]:
                posemb = posemb[:, 1:]
                self.transformer.embeddings.position_embeddings.copy_(posemb)
            else:
                logger.info("load_pretrained: resized variant: %s to %s" % (posemb.size(), posemb_new.size()))
                ntok_new = posemb_new.size(1)
                if self.classifier == "seg":
                    _, posemb_grid = posemb[:, :1], posemb[0, 1:]
                gs_old = int(np.sqrt(len(posemb_grid)))
                gs_new = int(np.sqrt(ntok_new))
                print('load_pretrained: grid-size from %s to %s' % (gs_old, gs_new))
                posemb_grid = posemb_grid.reshape(gs_old, gs_old, -1)
                zoom = (gs_new / gs_old, gs_new / gs_old, 1)
                posemb_grid = ndimage.zoom(posemb_grid, zoom, order=1)  # th2np
                posemb_grid = posemb_grid.reshape(1, gs_new * gs_new, -1)
                posemb = posemb_grid
                self.transformer.embeddings.position_embeddings.copy_(np2th(posemb))

            # Encoder whole
            for bname, block in self.transformer.encoder.named_children():
                for uname, unit in block.named_children():
                    unit.load_from(weights, n_block=uname)

            if self.transformer.embeddings.hybrid:
                self.transformer.embeddings.hybrid_model.root.conv.weight.copy_(np2th(res_weight["conv_root/kernel"], conv=True))
                gn_weight = np2th(res_weight["gn_root/scale"]).view(-1)
                gn_bias = np2th(res_weight["gn_root/bias"]).view(-1)
                self.transformer.embeddings.hybrid_model.root.gn.weight.copy_(gn_weight)
                self.transformer.embeddings.hybrid_model.root.gn.bias.copy_(gn_bias)

                for bname, block in self.transformer.embeddings.hybrid_model.body.named_children():
                    for uname, unit in block.named_children():
                        unit.load_from(res_weight, n_block=bname, n_unit=uname)

CONFIGS = {
    'ViT-B_16': configs.get_b16_config(),
    'ViT-B_32': configs.get_b32_config(),
    'ViT-L_16': configs.get_l16_config(),
    'ViT-L_32': configs.get_l32_config(),
    'ViT-H_14': configs.get_h14_config(),
    'R50-ViT-B_16': configs.get_r50_b16_config(),
    'R50-ViT-L_16': configs.get_r50_l16_config(),
    'testing': configs.get_testing(),
}


