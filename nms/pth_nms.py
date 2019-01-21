

import torch
from ._ext import nms

from tools.config import Config


def pth_nms(dets, thresh):
    """dets has to be a tensor"""
    x1 = dets[:, 1]
    y1 = dets[:, 0]
    x2 = dets[:, 3]
    y2 = dets[:, 2]
    scores = dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.sort(0, descending=True)[1]

    if not dets.is_cuda:
        keep = torch.LongTensor(dets.size(0))
        num_out = torch.LongTensor(1)
        nms.cpu_nms(keep, num_out, dets, order, areas, thresh)

        return keep[:num_out[0]]
    else:
        dets_temp = torch.zeros(dets.size(), dtype=torch.float32,
                                device=Config.DEVICE)
        dets_temp = dets[:, [1, 0, 3, 2, 4]]

        dets = dets[order].contiguous()

        keep = torch.LongTensor(dets.size(0))
        num_out = torch.LongTensor(1)

        nms.gpu_nms(keep, num_out, dets_temp, thresh)

        return order[keep[:num_out[0]].cuda()].contiguous()
