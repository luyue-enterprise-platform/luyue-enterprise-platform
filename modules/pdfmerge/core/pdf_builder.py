# -*- coding: utf-8 -*-
"""
PDF构建器 - 生成封面、目录，合并所有PDF
"""
import os
import io
import logging

logger = logging.getLogger('pdfmerge.builder')

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError('请安装 PyMuPDF: pip install PyMuPDF')


def generate_cover(cover_title, mode, company_name='', period_text=''):
    """
    生成封面PDF

    Args:
        cover_title: 封面标题（用户输入的名称）
        mode: 'refund' 或 'deduction'
        company_name: 公司名称（可选，从封面标题中提取或单独输入）
        period_text: 证明时间段文本（可选）

    Returns:
        str: 生成的封面PDF临时文件路径
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4尺寸 (pt)

    # 封面布局
    cx = 297.5  # 水平居中

    # 公司名称（上方）
    if company_name:
        _draw_centered_text(page, company_name, cx, 180, font_size=22, font_name='heiti', color=(0.1, 0.1, 0.1))

    # 主标题
    mode_text = '退税' if mode == 'refund' else '抵税'
    title_lines = cover_title.split('\n') if '\n' in cover_title else [cover_title]

    y = 300
    for line in title_lines:
        _draw_centered_text(page, line, cx, y, font_size=28, font_name='heiti', color=(0.8, 0.15, 0.1))
        y += 45

    # 副标题
    y += 20
    _draw_centered_text(page, f'重点群体税收优惠政策申报', cx, y, font_size=18, font_name='heiti', color=(0.1, 0.1, 0.1))
    y += 35
    _draw_centered_text(page, f'（{mode_text}）', cx, y, font_size=20, font_name='heiti', color=(0.8, 0.15, 0.1))
    y += 35
    _draw_centered_text(page, '备查资料', cx, y, font_size=22, font_name='heiti', color=(0.1, 0.1, 0.1))

    # 时间段
    if period_text:
        y += 60
        _draw_centered_text(page, period_text, cx, y, font_size=14, font_name='song', color=(0.3, 0.3, 0.3))

    # 生成日期
    from datetime import datetime
    date_str = datetime.now().strftime('%Y年%m月%d日')
    _draw_centered_text(page, date_str, cx, 720, font_size=14, font_name='song', color=(0.3, 0.3, 0.3))

    cover_path = os.path.join(os.path.dirname(doc.name) if doc.name else os.environ.get('TEMP', '/tmp'),
                              '_cover_tmp.pdf')
    # 使用临时文件
    import tempfile
    fd, cover_path = tempfile.mkstemp(suffix='.pdf', prefix='_cover_')
    os.close(fd)
    doc.save(cover_path)
    doc.close()

    logger.info(f'封面PDF生成完成: {cover_path}')
    return cover_path


def generate_toc(section_list, page_offsets):
    """
    生成目录PDF

    Args:
        section_list: [(section_name, start_page), ...] 各章节名称及起始页码
        page_offsets: 已计算的页码偏移

    Returns:
        str: 生成的目录PDF临时文件路径
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    cx = 297.5

    # 标题
    _draw_centered_text(page, '目录', cx, 50, font_size=22, font_name='heiti', color=(0.1, 0.1, 0.1))

    # 目录条目
    y = 100
    line_height = 28

    for section_name, start_page in section_list:
        if start_page is None:
            continue

        # 章节名称（左对齐）
        _draw_text(page, section_name, 60, y, font_size=12, font_name='song', color=(0.1, 0.1, 0.1))

        # 页码（右对齐）
        page_str = str(start_page)
        _draw_text(page, page_str, 520, y, font_size=12, font_name='song', color=(0.1, 0.1, 0.1))

        # 点线连接
        _draw_dotted_line(page, 60 + len(section_name) * 12 + 10, y - 3, 510, y - 3)

        y += line_height

        # 如果超过一页，新建页面
        if y > 780:
            page = doc.new_page(width=595, height=842)
            y = 50
            line_height = 28

    import tempfile
    fd, toc_path = tempfile.mkstemp(suffix='.pdf', prefix='_toc_')
    os.close(fd)
    doc.save(toc_path)
    doc.close()

    logger.info(f'目录PDF生成完成: {toc_path}')
    return toc_path


def merge_pdfs(pdf_paths, output_path, progress_callback=None):
    """
    合并多个PDF为一个

    Args:
        pdf_paths: PDF文件路径列表
        output_path: 输出文件路径
        progress_callback: 进度回调 callback(current, total, filename)

    Returns:
        str: 合并后的PDF路径
    """
    merged = fitz.open()

    total = len(pdf_paths)
    for i, pdf_path in enumerate(pdf_paths):
        if not os.path.isfile(pdf_path):
            logger.warning(f'PDF文件不存在，跳过: {pdf_path}')
            if progress_callback:
                progress_callback(i + 1, total, os.path.basename(pdf_path), False)
            continue

        try:
            src = fitz.open(pdf_path)
            merged.insert_pdf(src)
            src.close()
            logger.debug(f'合并: {os.path.basename(pdf_path)} ({src.page_count if "src" in dir() else "?"}页)')
        except Exception as e:
            logger.error(f'合并PDF失败 {pdf_path}: {e}')

        if progress_callback:
            progress_callback(i + 1, total, os.path.basename(pdf_path), True)

    merged.save(output_path)
    merged.close()

    logger.info(f'PDF合并完成: {output_path} ({total}个文件)')
    return output_path


def get_pdf_page_count(pdf_path):
    """获取PDF页数"""
    try:
        doc = fitz.open(pdf_path)
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 0


def _draw_centered_text(page, text, x, y, font_size=12, font_name='song', color=(0, 0, 0)):
    """绘制居中文字"""
    try:
        # 尝试使用内置中文字体
        if font_name == 'heiti':
            fontname = 'china-s'  # PyMuPDF内置简体宋体
        else:
            fontname = 'china-s'
    except Exception:
        fontname = 'helv'

    text_width = fitz.get_text_length(text, fontname=fontname, fontsize=font_size)
    actual_x = x - text_width / 2

    page.insert_text(
        fitz.Point(actual_x, y),
        text,
        fontname=fontname,
        fontsize=font_size,
        color=color,
    )


def _draw_text(page, text, x, y, font_size=12, font_name='song', color=(0, 0, 0)):
    """绘制左对齐文字"""
    try:
        if font_name == 'heiti':
            fontname = 'china-s'
        else:
            fontname = 'china-s'
    except Exception:
        fontname = 'helv'

    page.insert_text(
        fitz.Point(x, y),
        text,
        fontname=fontname,
        fontsize=font_size,
        color=color,
    )


def _draw_dotted_line(page, x1, y1, x2, y2):
    """绘制点线"""
    page.draw_line(
        fitz.Point(x1, y1),
        fitz.Point(x2, y2),
        color=(0.7, 0.7, 0.7),
        width=0.5,
        dashes=(1, 2),
    )
