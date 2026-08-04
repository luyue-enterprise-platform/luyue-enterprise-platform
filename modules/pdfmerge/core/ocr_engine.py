# -*- coding: utf-8 -*-
"""OCR 引擎封装 - 基于 rapidocr-onnxruntime

将图片中的文字识别为文本，用于与章节名目智能匹配。
"""
import os
import logging
import threading

logger = logging.getLogger(__name__)

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """单例模式获取 OCR 引擎（线程安全）"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR
                _engine = RapidOCR()
                logger.info('RapidOCR 引擎初始化完成')
    return _engine


def ocr_image(image_path):
    """对单张图片执行 OCR，返回识别到的完整文本

    Args:
        image_path: 图片文件路径或 numpy array

    Returns:
        str: 识别到的文字内容（各文本行用换行符连接）
    """
    try:
        engine = _get_engine()
        result, elapse = engine(image_path)
        if result:
            lines = [item[1] for item in result]
            return '\n'.join(lines)
        return ''
    except Exception as e:
        logger.warning(f'OCR 识别失败 {image_path}: {e}')
        return ''


def ocr_images(image_paths, progress_callback=None):
    """批量 OCR 识别

    Args:
        image_paths: 图片路径列表
        progress_callback: 可选回调 fn(current, total, current_path)

    Returns:
        list[str]: 每张图片对应的文本内容
    """
    results = []
    total = len(image_paths)
    for i, path in enumerate(image_paths):
        text = ocr_image(path)
        results.append(text)
        if progress_callback:
            progress_callback(i + 1, total, path)
    return results
