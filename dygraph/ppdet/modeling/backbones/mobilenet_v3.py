# copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from paddle import ParamAttr
from paddle.nn.functional.activation import hard_sigmoid, hard_swish
from paddle.regularizer import L2Decay
from ppdet.core.workspace import register, serializable
from numbers import Integral


def make_divisible(v, divisor=8, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class ConvBNLayer(nn.Layer):
    def __init__(self,
                 in_c,
                 out_c,
                 filter_size,
                 stride,
                 padding,
                 num_groups=1,
                 if_act=True,
                 act=None,
                 lr_mult=1.,
                 conv_decay=0.,
                 norm_type='bn',
                 norm_decay=0.,
                 freeze_norm=False,
                 name=""):
        super(ConvBNLayer, self).__init__()
        self.if_act = if_act
        self.act = act
        self.conv = nn.Conv2D(
            in_channels=in_c,
            out_channels=out_c,
            kernel_size=filter_size,
            stride=stride,
            padding=padding,
            groups=num_groups,
            weight_attr=ParamAttr(
                learning_rate=lr_mult, regularizer=L2Decay(conv_decay), name=name + "_weights"),
            bias_attr=False)
    
        norm_lr = 0. if freeze_norm else lr_mult
        if norm_type == 'sync_bn':
            batch_norm = nn.SyncBatchNorm
        else:
            batch_norm = nn.BatchNorm2D
        self.bn = batch_norm(
            out_c,
            weight_attr=ParamAttr(
                learning_rate=norm_lr, name=name + "_bn_scale", regularizer=L2Decay(norm_decay)),
            bias_attr=ParamAttr(
                learning_rate=norm_lr, name=name + "_bn_offset", regularizer=L2Decay(norm_decay)))

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        if self.if_act:
            if self.act == "relu":
                x = F.relu(x)
            elif self.act == "relu6":
                x = F.relu6(x)
            elif self.act == "hard_swish":
                x = hard_swish(x)
            else:
                raise NotImplementedError("The activation function is selected incorrectly.")
        return x


class ResidualUnit(nn.Layer):
    def __init__(self,
                 in_c,
                 mid_c,
                 out_c,
                 filter_size,
                 stride,
                 use_se,
                 lr_mult,
                 conv_decay=0.,
                 norm_type='bn',
                 norm_decay=0.,
                 freeze_norm=False,
                 act=None,
                 name=''):
        super(ResidualUnit, self).__init__()
        self.if_shortcut = stride == 1 and in_c == out_c
        self.if_se = use_se

        self.expand_conv = ConvBNLayer(
            in_c=in_c,
            out_c=mid_c,
            filter_size=1,
            stride=1,
            padding=0,
            if_act=True,
            act=act,
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_expand")
        self.bottleneck_conv = ConvBNLayer(
            in_c=mid_c,
            out_c=mid_c,
            filter_size=filter_size,
            stride=stride,
            padding=int((filter_size - 1) // 2),
            num_groups=mid_c,
            if_act=True,
            act=act,
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_depthwise")
        if self.if_se:
            self.mid_se = SEModule(mid_c, lr_mult, conv_decay, name=name + "_se")
        self.linear_conv = ConvBNLayer(
            in_c=mid_c,
            out_c=out_c,
            filter_size=1,
            stride=1,
            padding=0,
            if_act=False,
            act=None,
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_linear")

    def forward(self, inputs):
        x = self.expand_conv(inputs)
        x = self.bottleneck_conv(x)
        if self.if_se:
            x = self.mid_se(x)
        x = self.linear_conv(x)
        if self.if_shortcut:
            x = paddle.add(inputs, x)
        return x


class SEModule(nn.Layer):
    def __init__(self, channel, lr_mult, conv_decay, reduction=4, name=""):
        super(SEModule, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2D(1)
        mid_channels = int(channel // reduction)
        self.conv1 = nn.Conv2D(
            in_channels=channel,
            out_channels=mid_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            weight_attr=ParamAttr(
                learning_rate=lr_mult, regularizer=L2Decay(conv_decay), name=name + "_1_weights"),
            bias_attr=ParamAttr(
                learning_rate=lr_mult, regularizer=L2Decay(conv_decay), name=name + "_1_offset"))
        self.conv2 = nn.Conv2D(
            in_channels=mid_channels,
            out_channels=channel,
            kernel_size=1,
            stride=1,
            padding=0,
            weight_attr=ParamAttr(
                learning_rate=lr_mult, regularizer=L2Decay(conv_decay), name=name + "_2_weights"),
            bias_attr=ParamAttr(
                learning_rate=lr_mult, regularizer=L2Decay(conv_decay), name=name + "_2_offset"))

    def forward(self, inputs):
        outputs = self.avg_pool(inputs)
        outputs = self.conv1(outputs)
        outputs = F.relu(outputs)
        outputs = self.conv2(outputs)
        outputs = hard_sigmoid(outputs)
        return paddle.multiply(x=inputs, y=outputs)


class ExtraBlockDW(nn.Layer):
    def __init__(self,
                 in_c,
                 ch_1,
                 ch_2,
                 stride,
                 lr_mult,
                 conv_decay=0.,
                 norm_type='bn',
                 norm_decay=0.,
                 freeze_norm=False,
                 name=None):
        super(ExtraBlockDW, self).__init__()
        self.pointwise_conv = ConvBNLayer(
            in_c=in_c,
            out_c=ch_1,
            filter_size=1,
            stride=1,
            padding=0,
            if_act=True,
            act='relu6',
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_extra1")
        self.depthwise_conv = ConvBNLayer(
            in_c=ch_1,
            out_c=ch_2,
            filter_size=3,
            stride=stride,
            padding=1, 
            num_groups=int(ch_1),
            if_act=True,
            act='relu6',
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_extra2_dw")
        self.normal_conv = ConvBNLayer(
            in_c=ch_2,
            out_c=ch_2,
            filter_size=1,
            stride=1,
            padding=0,
            if_act=True,
            act='relu6',
            lr_mult=lr_mult,
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name=name + "_extra2_sep")

    def forward(self, inputs):
        x = self.pointwise_conv(inputs)
        x = self.depthwise_conv(x)
        x = self.normal_conv(x)
        return x


@register
@serializable
class MobileNetV3(nn.Layer):
    __shared__ = ['norm_type']

    def __init__(self,
                 scale=1.0,
                 model_name="large",
                 feature_maps=[6,12,15],
                 with_extra_blocks=False,
                 extra_block_filters=[[256, 512], [128, 256], [128, 256], [64, 128]],
                 lr_mult_list=[1.0, 1.0, 1.0, 1.0, 1.0],
                 conv_decay=0.0,
                 multiplier=1.0,
                 norm_type='sync_bn',
                 norm_decay=0.0,
                 freeze_norm=False):
        super(MobileNetV3, self).__init__()
        if isinstance(feature_maps, Integral):
            feature_maps = [feature_maps]
        if norm_type == 'sync_bn' and freeze_norm:
            raise ValueError(
                "The norm_type should not be sync_bn when freeze_norm is True")
        self.feature_maps = feature_maps
        self.with_extra_blocks = with_extra_blocks
        self.extra_block_filters = extra_block_filters

        inplanes = 16
        if model_name == "large":
            self.cfg = [
                # k, exp, c,  se,     nl,  s,
                [3, 16, 16, False, "relu", 1],
                [3, 64, 24, False, "relu", 2],
                [3, 72, 24, False, "relu", 1],
                [5, 72, 40, True, "relu", 2],
                [5, 120, 40, True, "relu", 1],
                [5, 120, 40, True, "relu", 1], #
                [3, 240, 80, False, "hard_swish", 2],
                [3, 200, 80, False, "hard_swish", 1],
                [3, 184, 80, False, "hard_swish", 1],
                [3, 184, 80, False, "hard_swish", 1],
                [3, 480, 112, True, "hard_swish", 1],
                [3, 672, 112, True, "hard_swish", 1], #
                [5, 672, 160, True, "hard_swish", 2],
                [5, 960, 160, True, "hard_swish", 1],
                [5, 960, 160, True, "hard_swish", 1], #
            ]
            self.cls_ch_squeeze = 960
            self.cls_ch_expand = 1280
        elif model_name == "small":
            self.cfg = [
                # k, exp, c,  se,     nl,  s,
                [3, 16, 16, True, "relu", 2],
                [3, 72, 24, False, "relu", 2],
                [3, 88, 24, False, "relu", 1], #
                [5, 96, 40, True, "hard_swish", 2],
                [5, 240, 40, True, "hard_swish", 1],
                [5, 240, 40, True, "hard_swish", 1],
                [5, 120, 48, True, "hard_swish", 1],
                [5, 144, 48, True, "hard_swish", 1], #
                [5, 288, 96, True, "hard_swish", 2],
                [5, 576, 96, True, "hard_swish", 1],
                [5, 576, 96, True, "hard_swish", 1], #
            ]
            self.cls_ch_squeeze = 576
            self.cls_ch_expand = 1280
        else:
            raise NotImplementedError(
                "mode[{}_model] is not implemented!".format(model_name))

        if multiplier != 1.0:
            self.cfg[-3][2] = int(self.cfg[-3][2] * multiplier)
            self.cfg[-2][1] = int(self.cfg[-2][1] * multiplier)
            self.cfg[-2][2] = int(self.cfg[-2][2] * multiplier)
            self.cfg[-1][1] = int(self.cfg[-1][1] * multiplier)
            self.cfg[-1][2] = int(self.cfg[-1][2] * multiplier)

        self.conv1 = ConvBNLayer(
            in_c=3,
            out_c=make_divisible(inplanes * scale),
            filter_size=3,
            stride=2,
            padding=1,
            num_groups=1,
            if_act=True,
            act="hard_swish",
            lr_mult=lr_mult_list[0],
            conv_decay=conv_decay,
            norm_type=norm_type,
            norm_decay=norm_decay,
            freeze_norm=freeze_norm,
            name="conv1")

        self.block_list = []
        i = 0
        curr_stage = 0
        inplanes = make_divisible(inplanes * scale)
        for (k, exp, c, se, nl, s) in self.cfg:
            lr_idx = min(curr_stage // 3, len(lr_mult_list) - 1)
            lr_mult = lr_mult_list[lr_idx]

            block = self.add_sublayer(
                "conv" + str(i + 2),
                sublayer=ResidualUnit(
                    in_c=inplanes,
                    mid_c=make_divisible(scale * exp),
                    out_c=make_divisible(scale * c),
                    filter_size=k,
                    stride=s,
                    use_se=se,
                    act=nl,
                    lr_mult=lr_mult,
                    conv_decay=conv_decay,
                    norm_type=norm_type,
                    norm_decay=norm_decay,
                    freeze_norm=freeze_norm,
                    name="conv" + str(i + 2)))
            self.block_list.append(block)
            inplanes = make_divisible(scale * c)
            i += 1
            curr_stage += 1

        if self.with_extra_blocks:
            self.extra_block_list = []
            extra_out_c = make_divisible(scale * self.cls_ch_squeeze)
            lr_idx = min(curr_stage // 3, len(lr_mult_list) - 1)
            lr_mult = lr_mult_list[lr_idx]

            conv_extra = self.add_sublayer(
                "conv"+ str(i + 2),
                sublayer=ConvBNLayer(
                    in_c=inplanes,
                    out_c=extra_out_c,
                    filter_size=1,
                    stride=1,
                    padding=0,
                    num_groups=1,
                    if_act=True,
                    act="hard_swish",
                    lr_mult=lr_mult,
                    conv_decay=conv_decay,
                    norm_type=norm_type,
                    norm_decay=norm_decay,
                    freeze_norm=freeze_norm,
                    name="conv"+ str(i + 2)))
            self.extra_block_list.append(conv_extra)
            i += 1

            for j, block_filter in enumerate(self.extra_block_filters):
                in_c = extra_out_c if j == 0 else self.extra_block_filters[j-1][1]
                conv_extra = self.add_sublayer(
                    "conv"+ str(i + 2),
                    sublayer=ExtraBlockDW(
                        in_c,
                        block_filter[0],
                        block_filter[1],
                        stride=2,
                        lr_mult=lr_mult,
                        conv_decay=conv_decay,
                        norm_type=norm_type,
                        norm_decay=norm_decay,
                        freeze_norm=freeze_norm,
                        name='conv' + str(i + 2)))
                self.extra_block_list.append(conv_extra)
                i += 1


    def forward(self, inputs):
        x = self.conv1(inputs['image'])
        outs = []
        for idx, block in enumerate(self.block_list):
            x = block(x)
            if idx + 1 in self.feature_maps:
                outs.append(x)

        if not self.with_extra_blocks:
            return outs

        x = outs[-1]
        for i, block in enumerate(self.extra_block_list):
            idx = i + len(self.block_list)
            x = block(x)
            if idx + 1 in self.feature_maps:
                outs.append(x)
        return outs

