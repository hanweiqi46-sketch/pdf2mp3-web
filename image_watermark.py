#!/usr/bin/env python3
"""
图片去水印模块
使用 OpenCV inpainting 修复用户框选的水印区域
"""

import cv2
import numpy as np


def remove_image_watermark(input_path, output_path, regions):
    """
    对图片中用户指定的区域做 inpainting 修复（去除水印）。
    参数：
        input_path  - 原始图片路径
        output_path - 输出图片路径
        regions     - 水印区域列表，每项为 {x, y, w, h}（像素坐标）
    """
    img = cv2.imread(input_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("无法读取图片，请检查文件格式")

    # 如果是带 alpha 通道（RGBA），先转 BGR 处理
    has_alpha = img.shape[2] == 4 if img.ndim == 3 else False
    if has_alpha:
        alpha_channel = img[:, :, 3]
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    # 构建 mask：水印区域为白色（255），其余为黑色（0）
    mask = np.zeros(img.shape[:2], dtype=np.uint8)
    for r in regions:
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        # 边界安全检查
        x = max(0, x); y = max(0, y)
        w = min(w, img.shape[1] - x)
        h = min(h, img.shape[0] - y)
        if w > 0 and h > 0:
            mask[y:y+h, x:x+w] = 255

    # 用 Telea 算法做 inpainting（效果比 NS 更平滑）
    result = cv2.inpaint(img, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)

    # 恢复 alpha 通道
    if has_alpha:
        result = cv2.cvtColor(result, cv2.COLOR_BGR2BGRA)
        result[:, :, 3] = alpha_channel

    cv2.imwrite(output_path, result)
