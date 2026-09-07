# -*- coding: utf-8 -*-
"""A4 页面规范化引擎（v2.1.0 新增）

把任意 PDF 逐页映射到 A4 页面：
- 逐页检测方向：竖版页 → A4 竖版(595.3×841.9pt)，横版页 → A4 横版(841.9×595.3pt)，
  方向绝不改变；
- 内容等比缩放（保持宽高比，不变形）并居中放置，保证不截断、不错位；
- 文字保持矢量（不栅格化），后续 pdf2docx 仍可提取真实字体/字号/颜色。

所有页面尺寸统一为 A4，为后续转换与校验提供稳定基准。
"""
import os

import fitz  # PyMuPDF

# A4 尺寸（pt）：210×297mm
A4_W, A4_H = fitz.paper_size('a4')          # (595.276, 841.89)
A4_PORTRAIT = (A4_W, A4_H)
A4_LANDSCAPE = (A4_H, A4_W)

# 方向判定与尺寸校验容差（pt）
ORIENT_TOL = 2.0
SIZE_TOL = 3.0


def page_orientation(width, height):
    """按页面宽高判定方向：'portrait' | 'landscape'（正方形按竖版处理）"""
    return 'landscape' if width > height + ORIENT_TOL else 'portrait'


def a4_size_for(orientation):
    return A4_LANDSCAPE if orientation == 'landscape' else A4_PORTRAIT


def is_a4(width, height):
    """是否任一方向的 A4（±容差）"""
    for w, h in (A4_PORTRAIT, A4_LANDSCAPE):
        if abs(width - w) <= SIZE_TOL and abs(height - h) <= SIZE_TOL:
            return True
    return False


def expected_orientations(pdf_path):
    """逐页方向列表（原文件方向基线）：['portrait','landscape',...]"""
    out = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            r = page.rect
            out.append(page_orientation(r.width, r.height))
    return out


def orientation_runs(orientations):
    """方向序列压缩为 run-length 段：[(orientation, start_page_1based, end_page_1based)]"""
    runs = []
    for i, o in enumerate(orientations):
        if runs and runs[-1][0] == o:
            runs[-1] = (o, runs[-1][1], i + 1)
        else:
            runs.append((o, i + 1, i + 1))
    return runs


def normalize_pdf_to_a4(src, dst, margin_pt=0.0):
    """把 src PDF 规范化为全 A4 PDF 写入 dst。

    每页：按原页方向选 A4 竖/横，内容等比缩放（scale = min(适配比例)）居中，
    不变形、不截断。margin_pt 预留页边距（默认 0，尽量 1:1 复现原布局）。

    返回: dict {
        'pages': 页数,
        'orientations': 规范化后逐页方向（与原文件一致）,
        'scales': 逐页缩放系数（校验/日志用）,
    }
    """
    if not os.path.isfile(src):
        raise ValueError('PDF 文件不存在: %s' % src)
    out_doc = fitz.open()
    orientations, scales = [], []
    try:
        with fitz.open(src) as src_doc:
            for page in src_doc:
                r = page.rect
                src_w, src_h = r.width, r.height
                if src_w <= 0 or src_h <= 0:
                    raise ValueError('存在尺寸异常的页面（宽或高为 0）')
                orient = page_orientation(src_w, src_h)
                dst_w, dst_h = a4_size_for(orient)
                new_page = out_doc.new_page(width=dst_w, height=dst_h)
                # 等比缩放：取宽高两个方向适配比例的较小值（保证完整落入 A4，不截断）
                avail_w = dst_w - 2 * margin_pt
                avail_h = dst_h - 2 * margin_pt
                scale = min(avail_w / src_w, avail_h / src_h)
                # 居中放置
                show_w = src_w * scale
                show_h = src_h * scale
                x0 = (dst_w - show_w) / 2.0
                y0 = (dst_h - show_h) / 2.0
                target = fitz.Rect(x0, y0, x0 + show_w, y0 + show_h)
                new_page.show_pdf_page(target, src_doc, page.number)
                orientations.append(orient)
                scales.append(scale)
        out_doc.save(dst, garbage=3, deflate=True)
    finally:
        out_doc.close()
    return {
        'pages': len(orientations),
        'orientations': orientations,
        'scales': scales,
    }
