# -*- coding: utf-8 -*-
"""
PDF 转 Word 转换核心模块
基于 pdf2docx 库，保留原文档的文本、图片、表格和排版格式。

v2.1.0 保真管线：
1. A4 规范化（page_norm）：逐页方向保持（竖→A4竖/横→A4横），内容等比缩放居中，
   不变形/不截断/不错位，页面统一 A4；
2. pdf2docx 转换（字体/字号/颜色/粗斜体/表格/图片/页眉页脚保留）；
3. 逐项校验（validator）：方向/尺寸/文本/图片/表格/字体，失败自动修正并重验，
   仍不过则按文件+页码给出明确不一致项。
"""

import os
import logging

logger = logging.getLogger('pdf2word')


def convert_pdf_to_docx(pdf_path, docx_path, start=0, end=None, with_validation=True):
    """
    将单个 PDF 文件转换为 Word 文档（v2.1.0：A4 规范化 + 逐项校验）。

    Args:
        pdf_path:  PDF 源文件路径
        docx_path: 输出 .docx 文件路径
        start:     起始页码（0-based），默认 0
        end:       结束页码（0-based，不含），None 表示到最后一页
        with_validation: 转换后是否执行逐项校验（默认开启）

    Returns:
        dict: {
            'ok': bool,
            'pages': int,        # 转换的页数
            'error': str or None,
            'validation': dict or None,   # 校验报告（overall: pass/fixed/fail）
        }
    """
    try:
        from pdf2docx import Converter
    except ImportError:
        return {'ok': False, 'pages': 0, 'error': 'pdf2docx 库未安装', 'validation': None}

    if not os.path.isfile(pdf_path):
        return {'ok': False, 'pages': 0, 'error': f'PDF 文件不存在: {pdf_path}',
                'validation': None}

    # 确保输出目录存在
    out_dir = os.path.dirname(docx_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    norm_path = None
    try:
        from . import page_norm, validator
        import tempfile
        # 规范化中间文件放系统临时目录（不污染输出目录与下载 ZIP）
        fd, norm_path = tempfile.mkstemp(prefix='pdf2word_norm_', suffix='.pdf')
        os.close(fd)

        # ---- 步骤1：A4 规范化（方向保持 + 统一 A4 + 等比缩放居中） ----
        norm_info = page_norm.normalize_pdf_to_a4(pdf_path, norm_path)
        total_pages = norm_info['pages']
        src_orientations = norm_info['orientations']

        # ---- 步骤2：pdf2docx 转换规范化后的 PDF ----
        cv = Converter(norm_path)
        cv.convert(docx_path, start=start, end=end)
        cv.close()

        converted_pages = total_pages
        if end is not None:
            converted_pages = min(end, total_pages) - start
        else:
            converted_pages = total_pages - start

        # ---- 步骤3：逐项校验（自动修正+重验，输出校验报告） ----
        report = None
        if with_validation:
            report = validator.validate_pdf_to_word(
                norm_path, docx_path, src_orientations,
                os.path.basename(pdf_path), os.path.basename(docx_path))

        logger.info(f'转换完成: {os.path.basename(pdf_path)} -> {os.path.basename(docx_path)} '
                    f'({converted_pages} 页, 校验={report["overall"] if report else "跳过"})')
        return {'ok': True, 'pages': max(0, converted_pages), 'error': None,
                'validation': report}

    except Exception as e:
        logger.error(f'转换失败 {pdf_path}: {e}')
        return {'ok': False, 'pages': 0, 'error': str(e), 'validation': None}

    finally:
        # 规范化中间文件：校验完成后清理（失败也尽量清；沙箱环境拦截则留临时目录）
        if norm_path and os.path.exists(norm_path):
            try:
                os.remove(norm_path)
            except OSError:
                pass


def batch_convert(pdf_files, output_dir, progress_callback=None):
    """
    批量转换 PDF 为 Word（v2.1.0：每个文件附校验报告）。

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
            'validation': dict or None,   # 逐项校验报告
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
            'validation': result.get('validation'),
        }
        results.append(entry)

        if progress_callback:
            progress_callback(i + 1, total, pdf_name, entry)

    return results
