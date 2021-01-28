import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from paddle import ParamAttr
from paddle.regularizer import L2Decay
from ppdet.core.workspace import register
# from ..backbones.darknet import ConvBNLayer


def _de_sigmoid(x, eps=1e-7):
    x = paddle.clip(x, eps, 1. / eps)
    x = paddle.clip(1. / x - 1., eps, 1. / eps)
    x = -paddle.log(x)
    return x


@register
class JDEHead(nn.Layer):
    __shared__ = ['num_classes']
    __inject__ = ['yolo_loss'] #, 'embedding_loss']

    def __init__(self,
                 anchors=[[10, 13], [16, 30], [33, 23], [30, 61], [62, 45],
                          [59, 119], [116, 90], [156, 198], [373, 326]],
                 anchor_masks=[[6, 7, 8], [3, 4, 5], [0, 1, 2]],
                 num_classes=1,
                 yolo_loss='YOLOv3Loss',
                 embedding_loss='CrossEntropyLoss',
                 embedding_dim=512,
                 iou_aware=False,
                 iou_aware_factor=0.4):
        super(JDEHead, self).__init__()
        self.num_classes = num_classes
        self.yolo_loss = yolo_loss
        self.embedding_loss = embedding_loss
        self.embedding_dim = embedding_dim

        self.iou_aware = iou_aware
        self.iou_aware_factor = iou_aware_factor

        self.parse_anchor(anchors, anchor_masks)
        self.num_outputs = len(self.anchors)

        self.yolo_outputs = []
        self.embedding_outputs = []
        for i in range(len(self.anchors)):
            if self.iou_aware:
                num_filters = self.num_outputs * (self.num_classes + 6)
            else:
                num_filters = self.num_outputs * (self.num_classes + 5)
            name = 'yolo_output.{}'.format(i)
            yolo_output = self.add_sublayer(
                name,
                nn.Conv2D(
                    in_channels=1024 // (2**i),
                    out_channels=num_filters,
                    kernel_size=1,
                    stride=1,
                    padding=0,
                    weight_attr=ParamAttr(name=name + '.conv.weights'),
                    bias_attr=ParamAttr(
                        name=name + '.conv.bias', regularizer=L2Decay(0.))))
            self.yolo_outputs.append(yolo_output)

            name = 'embedding_output.{}'.format(i)
            embedding_output = self.add_sublayer(
                name,
                nn.Conv2D( # activation=linear
                    in_channels=1024 // (2**(i+1)),
                    out_channels=embedding_dim,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    weight_attr=ParamAttr(name=name + '.conv.weights'),
                    bias_attr=ParamAttr(
                        name=name + '.conv.bias', regularizer=L2Decay(0.))))
            self.embedding_outputs.append(embedding_output)

    def parse_anchor(self, anchors, anchor_masks):
        self.anchors = [[anchors[i] for i in mask] for mask in anchor_masks]
        self.mask_anchors = []
        anchor_num = len(anchors)
        for masks in anchor_masks:
            self.mask_anchors.append([])
            for mask in masks:
                assert mask < anchor_num, "anchor mask index overflow"
                self.mask_anchors[-1].extend(anchors[mask])

    def forward(self, feats):
        assert len(feats) == len(self.anchors)
        yolo_outputs = []
        for i, feat in enumerate(feats):
            yolo_output = self.yolo_outputs[i](feat)
            yolo_outputs.append(yolo_output)
        return yolo_outputs

    def get_loss(self, inputs, targets):
        yolo_loss = self.yolo_loss(inputs, targets, self.anchors)
        # embedding_loss = self.embedding_loss(inputs, targets, self.anchors)
        return yolo_loss

    def get_outputs(self, outputs):
        if self.iou_aware:
            y = []
            for i, out in enumerate(outputs):
                na = len(self.anchors[i])
                ioup, x = out[:, 0:na, :, :], out[:, na:, :, :]
                b, c, h, w = x.shape
                no = c // na
                x = x.reshape((b, na, no, h * w))
                ioup = ioup.reshape((b, na, 1, h * w))
                obj = x[:, :, 4:5, :]
                ioup = F.sigmoid(ioup)
                obj = F.sigmoid(obj)
                obj_t = (obj**(1 - self.iou_aware_factor)) * (
                    ioup**self.iou_aware_factor)
                obj_t = _de_sigmoid(obj_t)
                loc_t = x[:, :, :4, :]
                cls_t = x[:, :, 5:, :]
                y_t = paddle.concat([loc_t, obj_t, cls_t], axis=2)
                y_t = y_t.reshape((b, c, h, w))
                y.append(y_t)
            return y
        else:
            return outputs
