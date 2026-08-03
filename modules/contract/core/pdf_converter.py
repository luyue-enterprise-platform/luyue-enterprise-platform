"""PDF转图片模块"""

import os
import logging

logger = logging.getLogger(__name__)


def pdf_to_images(pdf_path, output_dir=None, dpi=200):
    """将PDF每页渲染为PNG图片
    
    Args:
        pdf_path: PDF文件路径
        output_dir: 输出目录（默认与PDF同目录）
        dpi: 渲染分辨率，默认200 DPI
    
    Returns:
        list[str]: 生成的图片路径列表
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError('请安装 PyMuPDF: pip install PyMuPDF')
    
    if output_dir is None:
        output_dir = os.path.dirname(pdf_path)
    
    os.makedirs(output_dir, exist_ok=True)
    
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    
    doc = fitz.open(pdf_path)
    page_count = doc.page_count
    image_paths = []
    
    for page_num in range(page_count):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat)
        
        img_filename = f'{pdf_name}_p{page_num + 1}.png'
        img_path = os.path.join(output_dir, img_filename)
        pix.save(img_path)
        image_paths.append(img_path)
    
    doc.close()
    logger.info(f'PDF转换完成: {pdf_path} -> {len(image_paths)} 页图片')
    return image_paths
