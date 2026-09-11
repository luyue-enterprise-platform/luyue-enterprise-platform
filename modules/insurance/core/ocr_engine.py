# -*- coding: utf-8 -*-
"""OCR引擎模块 - 封装rapidocr，提供图片文字识别能力"""
import os
import sys
import threading

# 单文件 exe 模式下，确保 onnxruntime/capi 目录在 DLL 搜索路径中
if getattr(sys, 'frozen', False):
    try:
        meipass = sys._MEIPASS
        onnx_capi_dir = os.path.join(meipass, 'onnxruntime', 'capi')
        if os.path.isdir(onnx_capi_dir) and hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(onnx_capi_dir)
    except Exception:
        pass

# v2.3.2 并行 OCR：线程本地引擎实例——每线程首次调用时各自加载一次模型，
# 规避 InferenceSession 跨线程并发调用的兼容性风险；onnxruntime 推理
# 释放 GIL，多线程可真正并行。串行调用方（单线程）行为与原版完全一致。
_engines = threading.local()

# v2.3.3 线程数封顶：onnxruntime 会话默认 intra_op=物理核数（吃满全部核），
# 并行 N 个引擎就是 N×核数 个线程 spin-wait 抢核——超订严重时比串行还慢。
# 并行调度前由 blueprint 按 workers 数设置每引擎线程数（总量 ≈ 物理核数）。
_intra_op_threads = None


def set_intra_op_threads(n):
    """设置后续创建引擎的每会话线程数（None = onnxruntime 默认，吃满全核）

    仅影响设置之后创建的引擎实例；已存在的线程本地引擎保持原配置。
    """
    global _intra_op_threads
    _intra_op_threads = n


def get_engine():
    """懒加载OCR引擎（线程本地单例，避免重复加载模型）

    v2.4.0 模型热更新：外置模型（manifest 校验通过）优先于内置基线；
    温更新约定——模型替换后须重启平台，才会走到这里加载新模型。
    """
    engine = getattr(_engines, 'engine', None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR
        kwargs = {}
        if _intra_op_threads:
            # UpdateParameters 按 det_/cls_/rec_ 前缀分发到三个子模型会话
            kwargs = {
                'det_intra_op_num_threads': _intra_op_threads,
                'cls_intra_op_num_threads': _intra_op_threads,
                'rec_intra_op_num_threads': _intra_op_threads,
            }
        # v2.4.0 模型热更新：外置模型目录（manifest 校验通过）优先于内置基线；
        # 校验失败/无外置模型 → 返回空 dict，引擎走 rapidocr 默认解析（内置基线）。
        # 温更新约定：模型替换后须重启平台才会走到这里加载新模型。
        try:
            from core.model_store import engine_model_kwargs
            kwargs.update(engine_model_kwargs())
        except Exception:
            pass  # 模型仓库异常不阻断 OCR：回退内置基线
        engine = RapidOCR(**kwargs)
        _engines.engine = engine
    return engine


def ocr_image(image_path):
    """
    对单张图片进行OCR识别

    Args:
        image_path: 图片文件路径

    Returns:
        list of dict: 每个元素包含 text, x, y, score
                      按 y 坐标排序（从上到下），同 y 按 x 排序（从左到右）
    """
    engine = get_engine()
    result, elapse = engine(image_path)

    if result is None or len(result) == 0:
        return []

    items = []
    for box, text, score in result:
        # box: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        x = box[0][0]
        y = box[0][1]
        items.append({
            'text': text.strip(),
            'x': x,
            'y': y,
            'score': score
        })

    # 按y坐标排序（从上到下），同y按x排序（从左到右）
    items.sort(key=lambda item: (round(item['y'] / 15), item['x']))
    return items


def ocr_to_lines(image_path):
    """
    对图片进行OCR，返回纯文本行列表（每行是该y坐标附近的文字拼接）

    Returns:
        list of str: 文本行列表
    """
    items = ocr_image(image_path)
    if not items:
        return []

    lines = []
    current_y = None
    current_parts = []

    for item in items:
        y_bucket = round(item['y'] / 15)
        if current_y is None or y_bucket == current_y:
            current_parts.append(item['text'])
            current_y = y_bucket
        else:
            if current_parts:
                lines.append(' '.join(current_parts))
            current_parts = [item['text']]
            current_y = y_bucket

    if current_parts:
        lines.append(' '.join(current_parts))

    return lines


def ocr_to_text(image_path):
    """
    对图片进行OCR，返回拼接的纯文本

    Returns:
        str: 全部文字内容
    """
    lines = ocr_to_lines(image_path)
    return '\n'.join(lines)


def pdf_to_images(pdf_path, output_dir=None, dpi=200):
    """
    将PDF每页渲染为PNG图片（供OCR使用）

    Args:
        pdf_path: PDF文件路径
        output_dir: 图片输出目录（默认与PDF同目录）
        dpi: 渲染DPI（默认200，平衡清晰度与速度）

    Returns:
        list of str: 生成的图片路径列表，每页一张
    """
    import fitz  # PyMuPDF

    if output_dir is None:
        output_dir = os.path.dirname(pdf_path)

    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    doc = fitz.open(pdf_path)
    image_paths = []

    zoom = dpi / 72.0  # PDF默认72 DPI
    mat = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat)
        img_path = os.path.join(output_dir, f'{pdf_name}_p{page_num + 1}.png')
        pix.save(img_path)
        image_paths.append(img_path)

    doc.close()
    return image_paths
