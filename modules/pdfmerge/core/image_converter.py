# -*- coding: utf-8 -*-
"""文件转图片转换器

将所有支持的文件格式统一转换为 PNG 图片：
- 图片 (JPG/PNG/BMP/TIFF) → 直接复制（非PNG则转换）
- PDF → PyMuPDF 逐页渲染为 PNG
- Word/Excel → 先转 PDF (format_converter) → 再逐页渲染为 PNG

每个文件生成一张或多张图片，每张图片作为一个独立的"页面"参与后续 OCR 匹配和 PDF 生成。
"""
import os
import shutil
import logging

logger = logging.getLogger('pdfmerge.image_converter')

# 支持的文件扩展名
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
WORD_EXTS = {'.doc', '.docx'}
EXCEL_EXTS = {'.xls', '.xlsx'}
PDF_EXTS = {'.pdf'}
SUPPORTED_EXTS = IMAGE_EXTS | WORD_EXTS | EXCEL_EXTS | PDF_EXTS

# 渲染 DPI（影响图片质量和文件大小）
RENDER_DPI = 200


def _normalize_path(path):
    """规范化 Windows 长路径"""
    path = os.path.normpath(path)
    if os.name == 'nt':
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if len(path) > 255 and not path.startswith('\\\\?\\'):
            path = '\\\\?\\' + path
    return path


def convert_to_images(file_path, output_dir, file_index=0):
    """将单个文件转换为图片列表

    Args:
        file_path: 源文件路径
        output_dir: 图片输出目录
        file_index: 文件序号（用于命名，避免冲突）

    Returns:
        list[dict]: 图片信息列表
            [{path, original_filename, page_num, source_ext}, ...]
            失败返回空列表
    """
    file_path = _normalize_path(file_path)
    if not os.path.isfile(file_path):
        logger.warning(f'文件不存在: {file_path}')
        return []

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        logger.info(f'跳过不支持的格式: {ext} ({os.path.basename(file_path)})')
        return []

    basename = os.path.splitext(os.path.basename(file_path))[0]
    # 清理文件名中的特殊字符
    safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in basename)[:40]
    prefix = f'{file_index:03d}_{safe_name}'

    try:
        if ext in IMAGE_EXTS:
            return _image_to_png(file_path, output_dir, prefix, ext)
        elif ext in PDF_EXTS:
            return _pdf_to_images(file_path, output_dir, prefix)
        elif ext in WORD_EXTS or ext in EXCEL_EXTS:
            return _office_to_images(file_path, output_dir, prefix, ext)
    except Exception as e:
        logger.error(f'转换失败 {os.path.basename(file_path)}: {e}', exc_info=True)
        return []


def _image_to_png(image_path, output_dir, prefix, ext):
    """图片文件直接复制/转换为 PNG"""
    out_path = os.path.join(output_dir, f'{prefix}.png')

    if ext == '.png':
        # PNG 直接复制
        shutil.copy2(image_path, out_path)
    else:
        # 其他格式用 Pillow 转 PNG
        from PIL import Image
        img = Image.open(_normalize_path(image_path))
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            # 保持透明度
            pass
        else:
            img = img.convert('RGB')
        img.save(out_path, 'PNG')
        img.close()

    logger.info(f'图片复制: {os.path.basename(image_path)}')
    return [{
        'path': out_path,
        'original_filename': os.path.basename(image_path),
        'page_num': 1,
        'source_ext': ext,
    }]


def _pdf_to_images(pdf_path, output_dir, prefix):
    """PDF 逐页渲染为 PNG 图片"""
    import fitz

    pdf_path = _normalize_path(pdf_path)
    doc = fitz.open(pdf_path)
    images = []
    zoom = RENDER_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=matrix)
        out_path = os.path.join(output_dir, f'{prefix}_p{page_num + 1}.png')
        pix.save(out_path)
        images.append({
            'path': out_path,
            'original_filename': os.path.basename(pdf_path),
            'page_num': page_num + 1,
            'source_ext': '.pdf',
        })

    doc.close()
    logger.info(f'PDF转图片: {os.path.basename(pdf_path)} → {len(images)} 页')
    return images


def _office_to_images(office_path, output_dir, prefix, ext):
    """Word/Excel → PDF → 图片"""
    from .format_converter import convert_to_pdf

    # 先转 PDF
    pdf_path = convert_to_pdf(office_path, output_dir)
    if not pdf_path or not os.path.isfile(pdf_path):
        logger.warning(f'Office转PDF失败: {os.path.basename(office_path)}')
        return []

    # 再转图片
    images = _pdf_to_images(pdf_path, output_dir, prefix)

    # 删除中间 PDF 文件
    try:
        os.remove(pdf_path)
    except Exception:
        pass

    # 更新 source_ext
    for img in images:
        img['source_ext'] = ext

    return images


def scan_folder_recursive(folder_path):
    """递归扫描文件夹，返回所有支持的文件路径

    Args:
        folder_path: 文件夹路径

    Returns:
        list[str]: 文件路径列表
    """
    folder_path = _normalize_path(folder_path)
    files = []
    for root, dirs, filenames in os.walk(folder_path):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in SUPPORTED_EXTS:
                files.append(os.path.join(root, filename))
    return files


def check_conversion_capability():
    """检查系统的文件转换能力"""
    from .format_converter import check_conversion_capability as check_office
    caps = check_office()
    return {
        'image': True,
        'pdf': True,
        'word': caps.get('word', False),
        'excel': caps.get('excel', False),
        'method': caps.get('method', None),
    }
