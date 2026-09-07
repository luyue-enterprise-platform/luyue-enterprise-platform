# -*- coding: utf-8 -*-
"""图片主体内容方向判定（v2.1.2 新增）

修复：图片转 PDF 的横竖版此前按"像素宽高比"判定，对手机拍照的证件类图片
（竖版内容常因 EXIF 方向标记以横版像素存储）会被误判为横版——直观表现为
"以图片右下角水印/印章位置当了方向基准"，竖版内容被错误转成横版。

本模块改为"依据图片实际主体内容"判定，三级策略：
1. EXIF 方向归一化（ImageOps.exif_transpose）：先把按 EXIF 旋转标记存储的
   像素转正，消除拍照方向误判的根源；
2. OCR 主体内容判定：以全部文字框的外接矩形（并集）宽高比为准。文字主体
   成片区分布，孤立的右下角水印/印章小框不会显著改变整体宽高比，因此不会
   再被水印位置带偏；
3. 像素宽高比兜底：OCR 不可用或无文字（纯色/无文字照片）时，按 EXIF 归一
   化后的像素宽高判定，行为与旧逻辑一致，保证向后兼容。
"""
from . import page_norm

# OCR 方向判定前的降采样上限（仅加速，外接矩形宽高比不受缩放影响）
_OCR_MAX_DIM = 1280
# 文字框置信度下限（过滤低置信噪声框；对结果影响很小，因主体文字框占多数）
_SCORE_MIN = 0.5
# 内容"宽 > 高 × 该系数"即判横版（取 1.0：宽于高即横，与像素判定口径一致）
_LANDSCAPE_RATIO = 1.0


def _normalize_exif(img):
    """按 EXIF 方向标记转正像素；无 EXIF/失败时原样返回（不抛异常）"""
    from PIL import ImageOps
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _ocr_boxes(pil_img):
    """对 PIL 图片做 OCR，返回文字框外接矩形列表 [(x1,y1,x2,y2), ...]。

    任何一步失败（引擎不可用/识别异常/无文字）都返回 []，由调用方兜底。
    """
    try:
        from modules.insurance.core.ocr_engine import get_engine
        engine = get_engine()
    except Exception:
        return []
    im = pil_img
    try:
        w, h = im.size
        if max(w, h) > _OCR_MAX_DIM:
            s = _OCR_MAX_DIM / float(max(w, h))
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
        import numpy as np
        arr = np.array(im.convert('RGB'))
        result, _ = engine(arr)
    except Exception:
        return []
    if not result:
        return []
    boxes = []
    for item in result:
        try:
            box, _text, score = item
        except (TypeError, ValueError):
            continue
        if score is not None and score < _SCORE_MIN:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def content_orientation(pil_img):
    """依据主体文字内容返回 'portrait' | 'landscape'；无文字/OCR 不可用返回 None。

    以全部文字框的并集外接矩形宽高比为准：主体文字成片区，孤立水印小框
    不会主导整体宽高，因此对右下角水印/印章鲁棒。
    """
    boxes = _ocr_boxes(pil_img)
    if not boxes:
        return None
    min_x = min(b[0] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    cw, ch = max_x - min_x, max_y - min_y
    if cw <= 0 or ch <= 0:
        return None
    return 'landscape' if cw > ch * _LANDSCAPE_RATIO else 'portrait'


def decide_orientation(pil_img):
    """对外入口：判定图片应为横版还是竖版。

    返回 (归一化后的 PIL 图片, 'portrait'|'landscape', 判定依据)
      - 归一化后的图片：已按 EXIF 转正，调用方应使用它进行后续排版/渲染，
        保证输出内容与原始方向一致；
      - 判定依据：'content'（OCR 主体内容）或 'pixel'（像素宽高兜底）。
    """
    img = _normalize_exif(pil_img)
    orient = content_orientation(img)
    if orient is not None:
        return img, orient, 'content'
    w, h = img.size
    return img, page_norm.page_orientation(w, h), 'pixel'
