# -*- coding: utf-8 -*-
"""
PDF 转 Word 转换核心模块
基于 pdf2docx 库，保留原文档的文本、图片、表格和排版格式。
"""

import os
import logging

logger = logging.getLogger('pdf2word')


def convert_pdf_to_docx(pdf_path, docx_path, start=0, end=None):
    """
    将单个 PDF 文件转换为 Word 文档。

    Args:
        pdf_path:  PDF 源文件路径
        docx_path: 输出 .docx 文件路径
        start:     起始页码（0-based），默认 0
        end:       结束页码（0-based，不含），None 表示到最后一页

    Returns:
        dict: {
            'ok': bool,
            'pages': int,       # 转换的页数
            'error': str or None,
        }
    """
    try:
        from pdf2docx import Converter
    except ImportError:
        return {'ok': False, 'pages': 0, 'error': 'pdf2docx 库未安装'}

    if not os.path.isfile(pdf_path):
        return {'ok': False, 'pages': 0, 'error': f'PDF 文件不存在: {pdf_path}'}

    # 确保输出目录存在
    out_dir = os.path.dirname(docx_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        # 用 fitz 获取页数（可靠）
        import fitz
        doc_fitz = fitz.open(pdf_path)
        total_pages = doc_fitz.page_count
        doc_fitz.close()

        cv = Converter(pdf_path)
        cv.convert(docx_path, start=start, end=end)
        cv.close()

        converted_pages = total_pages
        if end is not None:
            converted_pages = min(end, total_pages) - start
        else:
            converted_pages = total_pages - start

        logger.info(f'转换完成: {os.path.basename(pdf_path)} -> {os.path.basename(docx_path)} ({converted_pages} 页)')
        return {'ok': True, 'pages': max(0, converted_pages), 'error': None}

    except Exception as e:
        logger.error(f'转换失败 {pdf_path}: {e}')
        return {'ok': False, 'pages': 0, 'error': str(e)}


def batch_convert(pdf_files, output_dir, progress_callback=None):
    """
    批量转换 PDF 为 Word。

    Args:
        pdf_files:        PDF 文件路径列表
        output_dir:       输出目录
        progress_callback: 可选回调 fn(current, total, filename, result)

    Returns:
        list[dict]: 每个文件的转换结果
        [{
            'pdf_name': str,
            'docx_name': str,
            'docx_path': str,
            'ok': bool,
            'pages': int,
            'error': str or None,
        }, ...]
    """
    os.makedirs(output_dir, exist_ok=True)
    results = []
    total = len(pdf_files)

    for i, pdf_path in enumerate(pdf_files):
        pdf_name = os.path.basename(pdf_path)
        docx_name = os.path.splitext(pdf_name)[0] + '.docx'
        docx_path = os.path.join(output_dir, docx_name)

        # 处理重名
        counter = 1
        while os.path.exists(docx_path) and docx_path != pdf_path:
            docx_name = f'{os.path.splitext(pdf_name)[0]}_{counter}.docx'
            docx_path = os.path.join(output_dir, docx_name)
            counter += 1

        result = convert_pdf_to_docx(pdf_path, docx_path)

        entry = {
            'pdf_name': pdf_name,
            'docx_name': docx_name,
            'docx_path': docx_path,
            'ok': result['ok'],
            'pages': result['pages'],
            'error': result['error'],
        }
        results.append(entry)

        if progress_callback:
            progress_callback(i + 1, total, pdf_name, entry)

    return results
