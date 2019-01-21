"""Handles Config class in configurations that are specific
to the MRCNN module."""

import math
import numpy as np

import torch

from tools.config import Config


def init_config(config_fns, device):
    """Loads configurations from YAML files, then create utilitaire
    configurations. Freeze config and display it.
    """
    Config.load_default()
    for filename in config_fns:
        Config.merge(filename)

    if Config.GPU_COUNT:
        Config.DEVICE = torch.device('cuda:' + str(device))

    if Config.GPU_COUNT > 0:
        Config.TRAINING.BATCH_SIZE = \
            Config.IMAGES_PER_GPU * Config.GPU_COUNT
    else:
        Config.TRAINING.BATCH_SIZE = Config.IMAGES_PER_GPU

    # Adjust step size based on batch size
    # Config.STEPS_PER_EPOCH = Config.BATCH_SIZE * Config.STEPS_PER_EPOCH
    Config.TRAINING.STEPS_PER_EPOCH = \
        ((657 - 25) // Config.IMAGES_PER_GPU)
    Config.TRAINING.VALIDATION_STEPS = 25 // Config.IMAGES_PER_GPU

    # Input image size
    Config.IMAGE.SHAPE = np.array(
        [Config.IMAGE.MAX_DIM, Config.IMAGE.MAX_DIM, 3])

    # Compute backbone size from input image size
    Config.BACKBONE.SHAPES = np.array(
        [[int(math.ceil(Config.IMAGE.SHAPE[0] / stride)),
          int(math.ceil(Config.IMAGE.SHAPE[1] / stride))]
         for stride in Config.BACKBONE.STRIDES])

    Config.RPN.BBOX_STD_DEV = torch.from_numpy(
        np.reshape(Config.RPN.BBOX_STD_DEV, [1, 4])
        ).float()

    Config.BBOX_STD_DEV = np.array(Config.BBOX_STD_DEV)

    check_config()
    Config.freeze()
    Config.display()


def check_config():
    """All configuration checks must be placed here."""
    # Image size must be dividable by 2 multiple times
    h, w = Config.IMAGE.SHAPE[:2]
    if h / 2**6 != int(h / 2**6) or w / 2**6 != int(w / 2**6):
        raise Exception("Image size must be divisable by 2 at least "
                        "6 times to avoid fractions when downscaling "
                        "and upscaling. For example, use 256, 320, 384, "
                        "448, 512, ... etc. ")
