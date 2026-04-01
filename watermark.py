#!/usr/bin/env python3
"""
PDF 去水印模块
使用 PyMuPDF (fitz) 去除 PDF 中的：
- Annotation 批注型水印
- 透明图片图层水印
"""

import os
import fitz  # PyMuPDF


def remove_watermark(input_path, output_path, log_fn):
    """
    去除 PDF 水印。
    参数：
        input_path  - 原始 PDF 路径
        output_path - 输出 PDF 路径
        log_fn      - 日志函数
    返回：True 表示成功
    """
    doc = fitz.open(input_path)
    total = len(doc)
    log_fn(f"📄 共 {total} 页，开始去水印处理…")

    removed_annots = 0
    removed_images = 0

    for page_num, page in enumerate(doc):
        # ── 1. 删除所有 Annotation 批注型水印 ──
        annots = list(page.annots())
        for annot in annots:
            page.delete_annot(annot)
            removed_annots += 1

        # ── 2. 删除透明图片图层（半透明水印图片）──
        img_list = page.get_images(full=True)
        for img_info in img_list:
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if not pix.alpha:
                    continue
                total_pixels = pix.width * pix.height
                if total_pixels == 0:
                    continue
                # 采样计算平均 alpha，避免遍历大图耗时
                sample_count = min(total_pixels, 2000)
                alpha_vals = [
                    pix.samples[i * pix.n + pix.n - 1]
                    for i in range(sample_count)
                ]
                avg_alpha = sum(alpha_vals) / len(alpha_vals)
                # avg_alpha < 180 (≈70%) 认为是半透明水印
                if avg_alpha < 180:
                    doc.xref_set_key(xref, "Width", "0")
                    doc.xref_set_key(xref, "Height", "0")
                    removed_images += 1
            except Exception:
                pass

        log_fn(f"   第 {page_num + 1}/{total} 页处理完成")

    log_fn(f"✅ 去水印完成：删除批注 {removed_annots} 个，透明图层 {removed_images} 个")
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log_fn(f"💾 输出文件：{size_mb:.1f} MB")
    return True
