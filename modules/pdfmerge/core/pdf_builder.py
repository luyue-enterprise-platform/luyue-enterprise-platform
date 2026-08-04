# -*- coding: utf-8 -*-
"""PDF 构建器 - 从图片池生成带封面、目录、页码的汇总 PDF

流程：
1. 创建内容页（每张图片一个PDF页，底部添加页码）
2. 计算各章节起始页码
3. 创建封面页
4. 创建目录页（含正确页码）
5. 合并为完整 PDF
"""
import os
import math
import logging
import tempfile

logger = logging.getLogger('pdfmerge.builder')

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError('请安装 PyMuPDF: pip install PyMuPDF')

# A4 尺寸（点）
A4_W = 595
A4_H = 842
# 页面边距
MARGIN = 30
# 页码区域高度
PAGE_NUM_HEIGHT = 20


def build_pdf(sections, output_path, cover_info, mode, progress_callback=None):
    """从匹配的图片章节构建完整 PDF

    Args:
        sections: 有序章节列表，每个章节含 images 列表
            [{id, name, year, images: [{id, path, ...}], ...}]
        output_path: 输出 PDF 路径
        cover_info: {title, company_name, period_text}
        mode: 'refund' 或 'deduction'
        progress_callback: fn(current, total, message)

    Returns:
        dict: {page_count, section_page_map, output_path}
    """
    from PIL import Image

    # 只处理有图片的章节
    active_sections = [s for s in sections if s.get('images')]

    if not active_sections:
        raise ValueError('没有匹配到任何图片，无法生成 PDF')

    # 第一步：创建内容 PDF（图片 + 页码）
    content_doc = fitz.open()
    section_page_map = []  # [(section_name, start_page, page_count)]
    current_page = 0  # 0-based content page index

    total_images = sum(len(s['images']) for s in active_sections)
    processed = 0

    for section in active_sections:
        start_page = current_page + 1  # 1-based page number

        for img_info in section['images']:
            img_path = img_info['path']
            if not os.path.isfile(img_path):
                logger.warning(f'图片不存在，跳过: {img_path}')
                processed += 1
                continue

            try:
                # 获取图片尺寸
                pil_img = Image.open(img_path)
                img_w, img_h = pil_img.size
                pil_img.close()

                # 判断横竖向，选择页面方向
                is_landscape = img_w > img_h * 1.2
                if is_landscape:
                    page_w, page_h = A4_H, A4_W  # 横向 A4
                else:
                    page_w, page_h = A4_W, A4_H  # 纵向 A4

                page = content_doc.new_page(width=page_w, height=page_h)

                # 计算图片放置区域（保持比例，居中）
                avail_w = page_w - 2 * MARGIN
                avail_h = page_h - 2 * MARGIN - PAGE_NUM_HEIGHT

                scale = min(avail_w / img_w, avail_h / img_h, 1.0)  # 不放大，只缩小
                if scale > 1.0:
                    scale = 1.0

                draw_w = img_w * scale
                draw_h = img_h * scale
                x = (page_w - draw_w) / 2
                y = (page_h - draw_h - PAGE_NUM_HEIGHT) / 2

                # 插入图片
                page.insert_image(fitz.Rect(x, y, x + draw_w, y + draw_h), filename=img_path)

                # 添加页码（底部居中）
                page_num = current_page + 1
                _draw_page_number(page, page_num, page_w, page_h)

                current_page += 1
            except Exception as e:
                logger.error(f'添加图片失败 {img_path}: {e}', exc_info=True)

            processed += 1
            if progress_callback:
                progress_callback(processed, total_images, f'添加图片 {processed}/{total_images}')

        page_count = current_page - (start_page - 1)
        section_page_map.append((section['name'], start_page, page_count))

    # 第二步：创建封面 PDF
    cover_path = _create_cover(cover_info, mode)

    # 第三步：创建目录 PDF
    toc_path = _create_toc(section_page_map)

    # 第四步：合并
    final_doc = fitz.open()

    # 封面
    cover_doc = fitz.open(cover_path)
    final_doc.insert_pdf(cover_doc)
    cover_doc.close()

    # 目录
    toc_doc = fitz.open(toc_path)
    final_doc.insert_pdf(toc_doc)
    toc_doc.close()

    # 内容
    final_doc.insert_pdf(content_doc)
    content_doc.close()

    # 保存
    final_doc.save(output_path)
    total_pages = final_doc.page_count
    final_doc.close()

    # 清理临时文件
    for tmp in [cover_path, toc_path]:
        try:
            os.remove(tmp)
        except Exception:
            pass

    logger.info(f'PDF 生成完成: {output_path} (总页数: {total_pages}, 内容页: {current_page})')

    return {
        'page_count': total_pages,
        'content_pages': current_page,
        'section_page_map': section_page_map,
        'output_path': output_path,
    }


def _create_cover(cover_info, mode):
    """创建封面 PDF"""
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    cx = A4_W / 2

    company_name = cover_info.get('company_name', '')
    title = cover_info.get('title', '')
    period_text = cover_info.get('period_text', '')
    mode_text = '退税' if mode == 'refund' else '抵税'

    # 公司名称
    y = 180
    if company_name:
        _draw_centered_text(page, company_name, cx, y, font_size=22, color=(0.1, 0.1, 0.1))
        y += 40

    # 主标题
    if title:
        _draw_centered_text(page, title, cx, 300, font_size=26, color=(0.8, 0.15, 0.1))

    # 副标题
    _draw_centered_text(page, '重点群体税收优惠政策申报', cx, 360, font_size=18, color=(0.1, 0.1, 0.1))
    _draw_centered_text(page, f'（{mode_text}）', cx, 395, font_size=20, color=(0.8, 0.15, 0.1))
    _draw_centered_text(page, '备查资料', cx, 430, font_size=22, color=(0.1, 0.1, 0.1))

    # 时间段
    if period_text:
        _draw_centered_text(page, period_text, cx, 510, font_size=14, color=(0.3, 0.3, 0.3))

    # 日期
    from datetime import datetime
    date_str = datetime.now().strftime('%Y年%m月%d日')
    _draw_centered_text(page, date_str, cx, 720, font_size=14, color=(0.3, 0.3, 0.3))

    fd, path = tempfile.mkstemp(suffix='.pdf', prefix='_cover_')
    os.close(fd)
    doc.save(path)
    doc.close()
    return path


def _create_toc(section_page_map):
    """创建目录 PDF

    Args:
        section_page_map: [(section_name, start_page, page_count), ...]
    """
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    cx = A4_W / 2

    # 标题
    _draw_centered_text(page, '目  录', cx, 50, font_size=22, color=(0.1, 0.1, 0.1))

    y = 100
    line_height = 28
    left_x = 60
    right_x = 520

    for section_name, start_page, page_count in section_page_map:
        # 截断过长的章节名
        display_name = section_name
        max_chars = 28
        if len(display_name) > max_chars:
            display_name = display_name[:max_chars] + '...'

        # 章节名称（左对齐）
        _draw_text(page, display_name, left_x, y, font_size=11, color=(0.1, 0.1, 0.1))

        # 页码（右对齐）
        page_str = str(start_page)
        page_width = fitz.get_text_length(page_str, fontname='china-s', fontsize=11)
        _draw_text(page, page_str, right_x - page_width, y, font_size=11, color=(0.1, 0.1, 0.1))

        # 点线连接
        name_width = fitz.get_text_length(display_name, fontname='china-s', fontsize=11)
        _draw_dotted_line(page, left_x + name_width + 8, y - 3, right_x - page_width - 8, y - 3)

        y += line_height

        # 新页面
        if y > 780:
            page = doc.new_page(width=A4_W, height=A4_H)
            y = 50

    fd, path = tempfile.mkstemp(suffix='.pdf', prefix='_toc_')
    os.close(fd)
    doc.save(path)
    doc.close()
    return path


def _draw_page_number(page, page_num, page_w, page_h):
    """在页面底部绘制页码"""
    text = f'- {page_num} -'
    fontname = 'china-s'
    font_size = 10
    text_width = fitz.get_text_length(text, fontname=fontname, fontsize=font_size)
    x = (page_w - text_width) / 2
    y = page_h - 15

    page.insert_text(
        fitz.Point(x, y),
        text,
        fontname=fontname,
        fontsize=font_size,
        color=(0.4, 0.4, 0.4),
    )


def _draw_centered_text(page, text, x, y, font_size=12, color=(0, 0, 0)):
    """绘制居中文字"""
    fontname = 'china-s'
    text_width = fitz.get_text_length(text, fontname=fontname, fontsize=font_size)
    actual_x = x - text_width / 2

    page.insert_text(
        fitz.Point(actual_x, y),
        text,
        fontname=fontname,
        fontsize=font_size,
        color=color,
    )


def _draw_text(page, text, x, y, font_size=12, color=(0, 0, 0)):
    """绘制左对齐文字"""
    page.insert_text(
        fitz.Point(x, y),
        text,
        fontname='china-s',
        fontsize=font_size,
        color=color,
    )


def _draw_dotted_line(page, x1, y1, x2, y2):
    """绘制点线"""
    if x2 <= x1:
        return
    page.draw_line(
        fitz.Point(x1, y1),
        fitz.Point(x2, y2),
        color=(0.7, 0.7, 0.7),
        width=0.5,
        dashes=(1, 2),
    )


def render_page_thumbnail(pdf_path, page_num_0indexed, max_width=200):
    """渲染指定页面的缩略图"""
    try:
        doc = fitz.open(pdf_path)
        if page_num_0indexed < 0 or page_num_0indexed >= doc.page_count:
            doc.close()
            return None

        page = doc[page_num_0indexed]
        zoom = max_width / page.rect.width if page.rect.width > 0 else 0.3
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        png_bytes = pix.tobytes("png")
        doc.close()
        return png_bytes
    except Exception as e:
        logger.error(f'渲染缩略图失败 (page={page_num_0indexed}): {e}')
        return None


def get_pdf_page_count(pdf_path):
    """获取PDF页数"""
    try:
        doc = fitz.open(pdf_path)
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 0
