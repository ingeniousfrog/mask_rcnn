"""
Mask R-CNN
Common utility functions and classes.

Copyright (c) 2017 Matterport, Inc.
Licensed under the MIT License (see LICENSE for details)
Written by Waleed Abdulla
"""

import math
import shutil
import random
import warnings
import urllib.request

import numpy as np
import scipy.misc
import scipy.ndimage
import skimage.transform
import torch

import torch.nn as nn
import torch.nn.functional as F

import mrcnn.config


COCO_MODEL_URL = "https://github.com/matterport/Mask_RCNN/releases/download/v2.0/mask_rcnn_coco.h5"


class NoPositiveAreaError(Exception):
    pass

############################################################
#  Bounding Boxes
############################################################


def apply_box_deltas(boxes, deltas):
    """Applies the given deltas to the given boxes.

    Args:
        boxes: [batch_size, N, 4] where each row is y1, x1, y2, x2
        deltas: [batch_size, N, 4] where each row is [dy, dx, log(dh), log(dw)]

    Returns:
        results: [batch_size, N, 4], where each row is [y1, x1, y2, x2]
    """
    # Convert to y, x, h, w
    height = boxes[:, :, 2] - boxes[:, :, 0]
    width = boxes[:, :, 3] - boxes[:, :, 1]
    center_y = boxes[:, :, 0] + 0.5 * height
    center_x = boxes[:, :, 1] + 0.5 * width
    # Apply deltas
    center_y = center_y + deltas[:, :, 0] * height
    center_x = center_x + deltas[:, :, 1] * width
    height = height * torch.exp(deltas[:, :, 2])
    width = width * torch.exp(deltas[:, :, 3])
    # Convert back to y1, x1, y2, x2
    y1 = center_y - 0.5 * height
    x1 = center_x - 0.5 * width
    y2 = y1 + height
    x2 = x1 + width
    result = torch.stack([y1, x1, y2, x2], dim=2)
    return result


def clip_to_window(window, boxes):
    """
        window: (y1, x1, y2, x2). The window in the image we want to clip to.
        boxes: [N, (y1, x1, y2, x2)]
    """
    y1, x1, y2, x2 = boxes.chunk(4, dim=1)
    y = torch.cat([y1, y2], dim=1).clamp(float(window[0]), float(window[2]))
    x = torch.cat([x1, x2], dim=1).clamp(float(window[1]), float(window[3]))
    boxes = torch.cat([y[:, 0].unsqueeze(1), x[:, 0].unsqueeze(1),
                       y[:, 1].unsqueeze(1), x[:, 1].unsqueeze(1)], dim=1)
    return boxes


def clip_boxes(boxes, window):
    """
    boxes: [N, 4] each col is y1, x1, y2, x2
    window: [4] in the form y1, x1, y2, x2
    """
    boxes = torch.stack(
        [boxes[:, :, 0].clamp(float(window[0]), float(window[2])),
         boxes[:, :, 1].clamp(float(window[1]), float(window[3])),
         boxes[:, :, 2].clamp(float(window[0]), float(window[2])),
         boxes[:, :, 3].clamp(float(window[1]), float(window[3]))], 2)
    return boxes


def extract_bboxes(mask):
    """Compute bounding boxes from masks.
    mask: [height, width, num_instances]. Mask pixels are either 1 or 0.

    Returns: bbox array [num_instances, (y1, x1, y2, x2)].
    """
    boxes = np.zeros([mask.shape[-1], 4], dtype=np.int32)
    for i in range(mask.shape[-1]):
        m = mask[:, :, i]
        # Bounding box.
        horizontal_indicies = np.where(np.any(m, axis=0))[0]
        vertical_indicies = np.where(np.any(m, axis=1))[0]
        if horizontal_indicies.shape[0]:
            x1, x2 = horizontal_indicies[[0, -1]]
            y1, y2 = vertical_indicies[[0, -1]]
            # x2 and y2 should not be part of the box. Increment by 1.
            x2 = x2 + 1
            y2 = y2 + 1
        else:
            # No mask for this instance. Might happen due to
            # resizing or cropping. Set bbox to zeros
            x1, x2, y1, y2 = 0, 0, 0, 0
        boxes[i] = np.array([y1, x1, y2, x2])
    return boxes.astype(np.int32)


def compute_iou(box, boxes, box_area, boxes_area):
    """Calculates IoU of the given box with the array of the given boxes.
    box: 1D vector [y1, x1, y2, x2]
    boxes: [boxes_count, (y1, x1, y2, x2)]
    box_area: float. the area of 'box'
    boxes_area: array of length boxes_count.

    Note: the areas are passed in rather than calculated here for
          efficency. Calculate once in the caller to avoid duplicate work.
    """
    # Calculate intersection areas
    y1 = np.maximum(box[0], boxes[:, 0])
    y2 = np.minimum(box[2], boxes[:, 2])
    x1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[3], boxes[:, 3])
    intersection = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    union = box_area + boxes_area[:] - intersection[:]
    iou = intersection / union
    return iou


def compute_overlaps(boxes1, boxes2):
    """Computes IoU overlaps between two sets of boxes.
    boxes1, boxes2: [N, (y1, x1, y2, x2)].

    For better performance, pass the largest set first and the smaller second.
    """
    # Areas of anchors and GT boxes
    area1 = (boxes1[:, 2] - boxes1[:, 0]) * (boxes1[:, 3] - boxes1[:, 1])
    area2 = (boxes2[:, 2] - boxes2[:, 0]) * (boxes2[:, 3] - boxes2[:, 1])

    # Compute overlaps to generate matrix [boxes1 count, boxes2 count]
    # Each cell contains the IoU value.
    overlaps = np.zeros((boxes1.shape[0], boxes2.shape[0]))
    for i in range(overlaps.shape[1]):
        box2 = boxes2[i]
        overlaps[:, i] = compute_iou(box2, boxes1, area2[i], area1)
    return overlaps


def box_refinement(box, gt_box):
    """Compute refinement needed to transform box to gt_box.
    box and gt_box are [N, (y1, x1, y2, x2)]
    """
    height = box[:, 2] - box[:, 0]
    width = box[:, 3] - box[:, 1]
    center_y = box[:, 0] + 0.5 * height
    center_x = box[:, 1] + 0.5 * width

    gt_height = gt_box[:, 2] - gt_box[:, 0]
    gt_width = gt_box[:, 3] - gt_box[:, 1]
    gt_center_y = gt_box[:, 0] + 0.5 * gt_height
    gt_center_x = gt_box[:, 1] + 0.5 * gt_width

    dy = (gt_center_y - center_y) / height
    dx = (gt_center_x - center_x) / width
    dh = torch.log(gt_height / height)
    dw = torch.log(gt_width / width)

    result = torch.stack([dy, dx, dh, dw], dim=1)
    return result


def subtract_mean(images, config):
    """Takes RGB images with 0-255 values and subtraces
    the mean pixel and converts it to float. Expects image
    colors in RGB order.
    """
    return images.astype(np.float32) - config.MEAN_PIXEL


def mold_image(image, config):
    """Takes RGB images with 0-255 values and subtraces
    the mean pixel and converts it to float. Expects image
    colors in RGB order.
    """
    molded_image, window, scale, padding, crop = resize_image(
        image,
        min_dim=config.IMAGE_MIN_DIM,
        max_dim=config.IMAGE_MAX_DIM,
        min_scale=config.IMAGE_MIN_SCALE,
        mode=config.IMAGE_RESIZE_MODE)
    molded_image = subtract_mean(molded_image, config)

    return molded_image, window, scale, padding, crop


def compose_image_meta(image_id, image_shape, window, active_class_ids):
    """Takes attributes of an image and puts them in one 1D array. Use
    parse_image_meta() to parse the values back.

    image_id: An int ID of the image. Useful for debugging.
    image_shape: [height, width, channels]
    window: (y1, x1, y2, x2) in pixels. The area of the image where the real
            image is (excluding the padding)
    active_class_ids: List of class_ids available in the dataset from which
        the image came. Useful if training on images from multiple datasets
        where not all classes are present in all datasets.
    """
    meta = np.array(
        [image_id]                # size=1
        + list(image_shape)       # size=3
        + list(window)            # size=4 (y1, x1, y2, x2) in image coordinates
        + list(active_class_ids)  # size=num_classes
    )
    return meta


def parse_image_meta(meta):
    """Parses an image info Numpy array to its components.
    See compose_image_meta() for more details.
    """
    image_id = meta[:, 0]
    image_shape = meta[:, 1:4]
    window = meta[:, 4:8]   # (y1, x1, y2, x2) window of image in in pixels
    active_class_ids = meta[:, 8:]
    return image_id, image_shape, window, active_class_ids


def mold_inputs(images, config):
    """Takes a list of images and modifies them to the format expected
    as an input to the neural network.
    images: List of image matricies [height,width,depth]. Images can have
        different sizes.

    Returns 3 Numpy matricies:
    molded_images: [N, h, w, 3]. Images resized and normalized.
    image_metas: [N, length of meta data]. Details about each image.
    windows: [N, (y1, x1, y2, x2)]. The portion of the image that has the
        original image (padding excluded).
    """
    molded_images = []
    image_metas = []
    windows = []
    for image in images:
        # Resize image to fit the model expected size
        molded_image, window, _, _, _ = mold_image(image, config)
        # Build image_meta
        image_meta = compose_image_meta(
            0, image.shape, window,
            np.zeros([config.NUM_CLASSES], dtype=np.int32))
        # Append
        molded_images.append(molded_image)
        windows.append(window)
        image_metas.append(image_meta)
    # Pack into arrays
    molded_images = np.stack(molded_images)
    image_metas = np.stack(image_metas)
    windows = np.stack(windows)
    return molded_images, image_metas, windows


def unmold_detections(detections, mrcnn_mask, image_shape, window):
    """Reformats the detections of one image from the format of the neural
    network output to a format suitable for use in the rest of the
    application.

    detections: [N, (y1, x1, y2, x2, class_id, score)]
    mrcnn_mask: [N, height, width, num_classes]
    image_shape: [height, width, depth] Original size of the image
                 before resizing
    window: [y1, x1, y2, x2] Box in the image where the real image is
            excluding the padding.

    Returns:
    boxes: [N, (y1, x1, y2, x2)] Bounding boxes in pixels
    class_ids: [N] Integer class IDs for each bounding box
    scores: [N] Float probability scores of the class_id
    masks: [height, width, num_instances] Instance masks
    """
    N = detections.shape[0]

    # Extract boxes, class_ids, scores, and class-specific masks
    boxes = detections[:N, :4]
    class_ids = detections[:N, 4].to(torch.long)
    scores = detections[:N, 5]
    masks = mrcnn_mask[torch.arange(N, dtype=torch.long), :, :, class_ids]

    return unmold_boxes(boxes, class_ids, masks, image_shape, window, scores)


def unmold_detections_x(detections, mrcnn_mask, image_shape, window):
    """Reformats the detections of one image from the format of the neural
    network output to a format suitable for use in the rest of the
    application.

    detections: [N, (y1, x1, y2, x2, class_id, score)]
    mrcnn_mask: [N, height, width, num_classes]
    image_shape: [height, width, depth] Original size of the image
                 before resizing
    window: [y1, x1, y2, x2] Box in the image where the real image is
            excluding the padding.

    Returns:
    boxes: [N, (y1, x1, y2, x2)] Bounding boxes in pixels
    class_ids: [N] Integer class IDs for each bounding box
    scores: [N] Float probability scores of the class_id
    masks: [height, width, num_instances] Instance masks
    """
    N = detections.shape[0]

    # Extract boxes, class_ids, scores, and class-specific masks
    boxes = detections[:N, :4]
    class_ids = detections[:N, 4].to(torch.long)
    scores = detections[:N, 5]
    masks = mrcnn_mask[torch.arange(N, dtype=torch.long), :, :, class_ids]

    return unmold_boxes_x(boxes, class_ids, masks, image_shape, window)


def unmold_boxes(boxes, class_ids, masks, image_shape, window, scores=None):
    """Reformats the detections of one image from the format of the neural
    network output to a format suitable for use in the rest of the
    application.

    detections: [N, (y1, x1, y2, x2, class_id, score)]
    masks: [N, height, width]
    image_shape: [height, width, depth] Original size of the image
                 before resizing
    window: [y1, x1, y2, x2] Box in the image where the real image is
            excluding the padding.

    Returns:
    boxes: [N, (y1, x1, y2, x2)] Bounding boxes in pixels
    class_ids: [N] Integer class IDs for each bounding box
    scores: [N] Float probability scores of the class_id
    masks: [height, width, num_instances] Instance masks
    """
    # Extract boxes, class_ids, scores, and class-specific masks
    class_ids = class_ids.to(torch.long)

    image_shape2 = (image_shape[0], image_shape[1])
    boxes = to_img_domain(boxes, window, image_shape).to(torch.int32)

    boxes, class_ids, masks, scores = remove_zero_area(boxes, class_ids,
                                                       masks, scores)

    full_masks = unmold_masks(masks, boxes, image_shape2)

    return boxes, class_ids, scores, full_masks


def unmold_boxes_x(boxes, class_ids, masks, image_shape, window, scores=None):
    """Reformats the detections of one image from the format of the neural
    network output to a format suitable for use in the rest of the
    application.

    detections: [N, (y1, x1, y2, x2, class_id, score)]
    masks: [N, height, width]
    image_shape: [height, width, depth] Original size of the image
                 before resizing
    window: [y1, x1, y2, x2] Box in the image where the real image is
            excluding the padding.

    Returns:
    boxes: [N, (y1, x1, y2, x2)] Bounding boxes in pixels
    class_ids: [N] Integer class IDs for each bounding box
    scores: [N] Float probability scores of the class_id
    masks: [height, width, num_instances] Instance masks
    """
    # Extract boxes, class_ids, scores, and class-specific masks
    class_ids = class_ids.to(torch.long)

    image_shape2 = (image_shape[0], image_shape[1])
    boxes = to_img_domain(boxes, window, image_shape)

    boxes, _, masks, _ = remove_zero_area(boxes, class_ids, masks)
    full_masks = unmold_masks_x(masks, boxes, image_shape2)

    return boxes, full_masks


def resize_image(image, min_dim=None, max_dim=None, min_scale=None,
                 mode="square"):
    """Resizes an image keeping the aspect ratio unchanged.

    min_dim: if provided, resizes the image such that it's smaller
        dimension == min_dim
    max_dim: if provided, ensures that the image longest side doesn't
        exceed this value.
    min_scale: if provided, ensure that the image is scaled up by at least
        this percent even if min_dim doesn't require it.
    mode: Resizing mode.
        none: No resizing. Return the image unchanged.
        square: Resize and pad with zeros to get a square image
            of size [max_dim, max_dim].
        pad64: Pads width and height with zeros to make them multiples of 64.
               If min_dim or min_scale are provided, it scales the image up
               before padding. max_dim is ignored in this mode.
               The multiple of 64 is needed to ensure smooth scaling of feature
               maps up and down the 6 levels of the FPN pyramid (2**6=64).
        crop: Picks random crops from the image. First, scales the image based
              on min_dim and min_scale, then picks a random crop of
              size min_dim x min_dim. Can be used in training only.
              max_dim is not used in this mode.

    Returns:
    image: the resized image
    window: (y1, x1, y2, x2). If max_dim is provided, padding might
        be inserted in the returned image. If so, this window is the
        coordinates of the image part of the full image (excluding
        the padding). The x2, y2 pixels are not included.
    scale: The scale factor used to resize the image
    padding: Padding added to the image [(top, bottom), (left, right), (0, 0)]
    """
    # Keep track of image dtype and return results in the same dtype
    image_dtype = image.dtype
    # Default window (y1, x1, y2, x2) and default scale == 1.
    h, w = image.shape[:2]
    window = (0, 0, h, w)
    scale = 1
    padding = [(0, 0), (0, 0), (0, 0)]
    crop = None

    if mode == "none":
        return image, window, scale, padding, crop

    # Scale?
    if min_dim and mode != "pad64":
        # Scale up but not down
        scale = max(1, min_dim / min(h, w))
    if min_scale:
        scale = max(scale, min_scale)
    if mode == "pad64":
        scale = min_dim/max(h, w)

    # Does it exceed max dim?
    if max_dim and mode == "square":
        image_max = max(h, w)
        if round(image_max * scale) > max_dim:
            scale = max_dim / image_max

    # Resize image using bilinear interpolation
    if scale != 1:
        image = skimage.transform.resize(
            image, (round(h * scale), round(w * scale)),
            order=1, mode="constant", preserve_range=True)

    # Need padding or cropping?
    h, w = image.shape[:2]
    if mode == "square":
        # Get new height and width
        top_pad = (max_dim - h) // 2
        bottom_pad = max_dim - h - top_pad
        left_pad = (max_dim - w) // 2
        right_pad = max_dim - w - left_pad
        padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
        image = np.pad(image, padding, mode='constant', constant_values=0)
        window = (top_pad, left_pad, h + top_pad, w + left_pad)
    elif mode == "pad64":
        # Both sides must be divisible by 64
        assert min_dim % 64 == 0, "Minimum dimension must be a multiple of 64"
        # Height
        if min_dim != h:
            # max_h = h - (min_dim % 64) + 64
            max_h = min_dim
            top_pad = (max_h - h) // 2
            bottom_pad = max_h - h - top_pad
        else:
            top_pad = bottom_pad = 0
        # Width
        if max_dim != w:
            # max_w = w - (max_dim % 64) + 64
            max_w = max_dim
            left_pad = (max_w - w) // 2
            right_pad = max_w - w - left_pad
        else:
            left_pad = right_pad = 0
        padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
        # TODO: zero is ok as padding value?
        image = np.pad(image, padding, mode='constant', constant_values=0)
        window = (top_pad, left_pad, h + top_pad, w + left_pad)
    elif mode == "crop":
        # Pick a random crop
        y = random.randint(0, (h - min_dim))
        x = random.randint(0, (w - min_dim))
        crop = (y, x, min_dim, min_dim)
        image = image[y:y + min_dim, x:x + min_dim]
        window = (0, 0, min_dim, min_dim)
    else:
        raise Exception("Mode {} not supported".format(mode))
    return image.astype(image_dtype), window, scale, padding, crop


def resize_mask(mask, scale, padding, crop=None):
    """Resizes a mask using the given scale and padding.
    Typically, you get the scale and padding from resize_image() to
    ensure both, the image and the mask, are resized consistently.

    scale: mask scaling factor
    padding: Padding to add to the mask in the form
            [(top, bottom), (left, right), (0, 0)]
    """
    # Suppress warning from scipy 0.13.0, the output shape of zoom() is
    # calculated with round() instead of int()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mask = scipy.ndimage.zoom(mask, zoom=[scale, scale, 1], order=0)
    if crop is not None:
        y, x, h, w = crop
        mask = mask[y:y + h, x:x + w]
    else:
        mask = np.pad(mask, padding, mode='constant', constant_values=0)
    return mask


def minimize_masks(boxes, masks, mini_shape):
    """Resize masks to a smaller version to cut memory load.
    Mini-masks can then resized back to image scale using expand_masks()

    See inspect_data.ipynb notebook for more details.
    """
    mini_masks = np.zeros(mini_shape + (masks.shape[-1],), dtype=bool)
    for i in range(masks.shape[-1]):
        m = masks[:, :, i].astype(bool)
        y1, x1, y2, x2 = boxes[i][:4]
        m = m[y1:y2, x1:x2]
        if m.size == 0:
            raise Exception("Invalid bounding box with area of zero")
        m = skimage.transform.resize(m, mini_shape, order=1, mode="constant")
        mini_masks[:, :, i] = np.around(m).astype(np.bool)
    return mini_masks


def expand_mask(bbox, mini_mask, image_shape):
    """Resizes mini masks back to image size. Reverses the change
    of minimize_mask().

    See inspect_data.ipynb notebook for more details.
    """
    mask = np.zeros(image_shape[:2] + (mini_mask.shape[-1],), dtype=bool)
    for i in range(mask.shape[-1]):
        m = mini_mask[:, :, i]
        y1, x1, y2, x2 = bbox[i][:4]
        h = y2 - y1
        w = x2 - x1
        m = scipy.misc.imresize(m.astype(float), (h, w), interp='bilinear')
        mask[y1:y2, x1:x2, i] = np.where(m >= 128, 1, 0)
    return mask


# TODO: Build and use this function to reduce code duplication
def mold_mask(mask, config):
    pass


def unmold_mask(mask, bbox, image_shape):
    """Converts a mask generated by the neural network into a format similar
    to its original shape.
    mask: [height, width] of type float. A small, typically 28x28 mask.
    bbox: [y1, x1, y2, x2]. The box to fit the mask in.

    Returns a binary mask with the same size as the original image.
    """
    threshold = 0.5
    y1, x1, y2, x2 = bbox
    shape = (y2 - y1, x2 - x1)

    mask = F.upsample(mask.unsqueeze(0).unsqueeze(0), size=shape,
                      mode='bilinear', align_corners=True)
    mask = mask.squeeze(0).squeeze(0)
    mask = torch.where(mask >= threshold,
                       torch.tensor(1, device=mrcnn.config.DEVICE),
                       torch.tensor(0, device=mrcnn.config.DEVICE))

    # Put the mask in the right location.
    full_mask = torch.zeros(image_shape[:2], dtype=torch.uint8)
    full_mask[y1:y2, x1:x2] = mask.to(torch.uint8)
    return full_mask


def unmold_masks(masks, boxes, image_shape):
    # Resize masks to original image size and set boundary threshold.
    N = masks.shape[0]
    full_masks = []
    for i in range(N):
        # Convert neural network mask to full size mask
        full_mask = unmold_mask(masks[i], boxes[i], image_shape)
        full_masks.append(full_mask)
    full_masks = torch.stack(full_masks, dim=-1)\
        if full_masks else torch.empty((0,) + masks.shape[1:3])
    return full_masks


def unmold_mask_x(mask, bbox, image_shape):
    """Converts a mask generated by the neural network into a format similar
    to its original shape.
    mask: [height, width] of type float. A small, typically 28x28 mask.
    bbox: [y1, x1, y2, x2]. The box to fit the mask in.

    Returns a binary mask with the same size as the original image.
    """
    # threshold = 0.5
    y1, x1, y2, x2 = bbox.floor()
    shape = (y2 - y1, x2 - x1)

    mask = F.upsample(mask.unsqueeze(0).unsqueeze(0), size=shape,
                      mode='bilinear', align_corners=True)
    mask = mask.squeeze(0).squeeze(0)
    mask = ((mask - 0.5)*100).sigmoid()
    return mask


def unmold_masks_x(masks, boxes, image_shape):
    # Resize masks to original image size and set boundary threshold.
    N = masks.shape[0]
    full_masks = []
    for i in range(N):
        # Convert neural network mask to full size mask
        full_mask = unmold_mask_x(masks[i], boxes[i], image_shape)
        full_masks.append(full_mask)
    return full_masks


def remove_zero_area(boxes, class_ids, masks, scores=None):
    # Filter out detections with zero area. Often only happens in early
    # stages of training when the network weights are still a bit random.
    dx, dy = boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]
    too_small = dx * dy <= 0.0
    too_short = dx <= 2.0
    too_thin = dy <= 2.0
    skip = too_small + too_short + too_thin
    positive_area = torch.nonzero(skip == 0)
    if positive_area.nelement() == 0:
        raise NoPositiveAreaError('No box has positive area.')
    keep_ix = positive_area[:, 0]
    if keep_ix.shape[0] != boxes.shape[0]:
        boxes = boxes[keep_ix]
        class_ids = class_ids[keep_ix]
        scores = scores[keep_ix] if scores is not None else None
        masks = masks[keep_ix]
    return boxes, class_ids, masks, scores


def to_img_domain(boxes, window, image_shape):
    image_shape = torch.tensor(image_shape, dtype=torch.float32,
                               device=mrcnn.config.DEVICE)
    window = torch.tensor(window, dtype=torch.float32,
                          device=mrcnn.config.DEVICE)
    # Compute scale and shift to translate coordinates to image domain.
    h_scale = image_shape[0] / (window[2] - window[0])
    w_scale = image_shape[1] / (window[3] - window[1])
    shift = window[:2]  # y, x
    scales = torch.tensor([h_scale, w_scale, h_scale, w_scale],
                          device=mrcnn.config.DEVICE)
    shifts = torch.tensor([shift[0], shift[1], shift[0], shift[1]],
                          device=mrcnn.config.DEVICE)

    # Translate bounding boxes to image domain
    boxes = ((boxes - shifts) * scales)
    return boxes

############################################################
#  Utilities
############################################################


def printProgressBar(iteration, total, losses):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
    """
    losses = losses.item()
    length = 10
    fill = '█'
    decimals = 1
    suffix = ("loss: {:.4f}, rpn_class: {:.4f}, "
              + "rpn_bbox: {:.4f}, mrcnn_class: {:.4f}, "
              + "mrcnn_bbox: {:.4f}, mrcnn_mask: {:.4f}")
    suffix = suffix.format(losses.total, losses.rpn_class,
                           losses.rpn_bbox, losses.mrcnn_class,
                           losses.mrcnn_bbox, losses.mrcnn_mask)

    percent = ("{0:." + str(decimals) + "f}")
    percent = percent.format(100 * (iteration / float(total)))

    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    prefix = "{}/{}".format(iteration, total)
    print('\r%s |%s| %s%% %s' % (prefix, bar, percent, suffix), end='\n')
    # Print New Line on Complete
    if iteration == total:
        print()


def download_trained_weights(coco_model_path, verbose=1):
    """Download COCO trained weights from Releases.

    coco_model_path: local path of COCO trained weights
    """
    if verbose > 0:
        print("Downloading pretrained model to " + coco_model_path + " ...")
    with urllib.request.urlopen(COCO_MODEL_URL) as resp, open(coco_model_path, 'wb') as out:
        shutil.copyfileobj(resp, out)
    if verbose > 0:
        print("... done downloading pretrained model!")


############################################################
#  Logging Utility Functions
############################################################


def log(text, array=None):
    """Prints a text message. And, optionally, if a Numpy array is provided it
    prints it's shape, min, and max values.
    """
    if array is not None:
        text = text.ljust(25)
        text += ("shape: {:20}  min: {:10.5f}  max: {:10.5f}".format(
            str(array.shape),
            array.min() if array.size else "",
            array.max() if array.size else ""))
    print(text)


############################################################
#  Pytorch Utility Functions
############################################################


def unique1d(tensor):
    if tensor.size()[0] == 0 or tensor.size()[0] == 1:
        return tensor
    tensor = tensor.sort()[0]
    unique_bool = tensor[1:] != tensor[:-1]
    first_element = torch.ByteTensor([True])
    first_element = first_element.to(mrcnn.config.DEVICE)
    unique_bool = torch.cat((first_element, unique_bool), dim=0)
    return tensor[unique_bool.detach()]


def intersect1d(tensor1, tensor2):
    aux = torch.cat((tensor1, tensor2), dim=0)
    aux = aux.sort()[0]
    return aux[:-1][(aux[1:] == aux[:-1]).detach()]


def get_printer(msg):
    """This function returns a printer function, that prints a message
    and then print a tensor. Used by register_hook in the backward pass.
    """
    def printer(tensor):
        if tensor.nelement() == 1:
            print(f"{msg} {tensor}")
        else:
            print(f"{msg} shape: {tensor.shape}"
                  f" max: {tensor.abs().max()} min: {tensor.abs().min()}"
                  f" mean: {tensor.abs().mean()}")
    return printer


def register_hook(tensor, msg):
    """Utility function to call retain_grad and Pytorch's register_hook
    in a single line
    """
    # return
    if not tensor.requires_grad:
        print(f"Tensor does not require grad. ({msg})")
        return
    tensor.retain_grad()
    tensor.register_hook(get_printer(msg))


class SamePad2d(nn.Module):
    """Mimics tensorflow's 'SAME' padding.
    """

    def __init__(self, kernel_size, stride):
        super(SamePad2d, self).__init__()
        self.kernel_size = torch.nn.modules.utils._pair(kernel_size)
        self.stride = torch.nn.modules.utils._pair(stride)

    def forward(self, input):
        in_width = input.size()[2]
        in_height = input.size()[3]
        out_width = math.ceil(float(in_width) / float(self.stride[-1]))
        out_height = math.ceil(float(in_height) / float(self.stride[1]))
        pad_along_width = ((out_width - 1) * self.stride[0] +
                           self.kernel_size[0] - in_width)
        pad_along_height = ((out_height - 1) * self.stride[1] +
                            self.kernel_size[1] - in_height)
        pad_left = math.floor(pad_along_width / 2)
        pad_top = math.floor(pad_along_height / 2)
        pad_right = pad_along_width - pad_left
        pad_bottom = pad_along_height - pad_top
        return F.pad(input, (pad_left, pad_right, pad_top, pad_bottom),
                     'constant', 0)

    def __repr__(self):
        return self.__class__.__name__


class TensorContainer():
    def to(self, device):
        for key, value in self.__dict__.items():
            self.__dict__[key] = value.to(device)
        return self

    def __str__(self):
        to_str = ''
        for key, tensor in self.__dict__.items():
            to_str += ' ' + key + ': ' + str(tensor.shape)
        return to_str

    def __len__(self):
        for key, tensor in self.__dict__.items():
            return tensor.shape[0]


class RPNOutput(TensorContainer):
    def __init__(self, class_logits, classes, deltas):
        self.class_logits = class_logits
        self.classes = classes
        self.deltas = deltas


class RPNTarget(TensorContainer):
    def __init__(self, match, deltas):
        self.match = match
        self.deltas = deltas


class MRCNNOutput(TensorContainer):
    def __init__(self, class_logits, deltas, masks):
        self.class_logits = class_logits
        self.deltas = deltas
        self.masks = masks


class MRCNNTarget():
    def __init__(self, class_ids, deltas, masks):
        self.class_ids = class_ids
        self.deltas = deltas
        self.masks = masks


class MRCNNGroundTruth(TensorContainer):
    def __init__(self, class_ids, boxes, masks):
        self.class_ids = class_ids
        self.boxes = boxes
        self.masks = masks


def get_empty_mrcnn_out():
    return MRCNNOutput(torch.FloatTensor(), torch.FloatTensor(),
                       torch.FloatTensor())