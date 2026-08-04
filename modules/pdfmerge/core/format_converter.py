# -*- coding: utf-8 -*-
"""
格式转换器 - 将各种文件格式转换为PDF
支持: 图片(JPG/PNG/BMP/TIFF) / Word(DOC/DOCX) / Excel(XLS/XLSX) / PDF
"""
import os
import logging
import tempfile
import subprocess

logger = logging.getLogger('pdfmerge.converter')

# 图片扩展名
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
# Word扩展名
WORD_EXTS = {'.doc', '.docx'}
# Excel扩展名
EXCEL_EXTS = {'.xls', '.xlsx'}


def convert_to_pdf(file_path, output_dir):
    """
    将任意支持的文件格式转换为PDF

    Args:
        file_path: 源文件路径
        output_dir: PDF输出目录

    Returns:
        str: 生成的PDF文件路径, 失败返回None
    """
    if not os.path.isfile(file_path):
        logger.error(f'文件不存在: {file_path}')
        return None

    ext = os.path.splitext(file_path)[1].lower()
    basename = os.path.splitext(os.path.basename(file_path))[0]
    pdf_path = os.path.join(output_dir, f'{basename}.pdf')

    # 避免文件名冲突
    counter = 1
    while os.path.exists(pdf_path):
        pdf_path = os.path.join(output_dir, f'{basename}_{counter}.pdf')
        counter += 1

    try:
        if ext == '.pdf':
            # PDF直接复制
            import shutil
            shutil.copy2(file_path, pdf_path)
            logger.info(f'PDF直接复制: {os.path.basename(file_path)}')
            return pdf_path

        elif ext in IMAGE_EXTS:
            return _image_to_pdf(file_path, pdf_path)

        elif ext in WORD_EXTS:
            return _word_to_pdf(file_path, pdf_path)

        elif ext in EXCEL_EXTS:
            return _excel_to_pdf(file_path, pdf_path)

        else:
            logger.warning(f'不支持的文件格式: {ext} ({file_path})')
            return None

    except Exception as e:
        logger.error(f'转换失败 {file_path}: {e}')
        return None


def _image_to_pdf(image_path, pdf_path):
    """将图片转换为PDF"""
    import fitz

    doc = fitz.open()
    img = fitz.open(image_path)
    page = doc.new_page(width=img[0].rect.width, height=img[0].rect.height)
    page.insert_image(page.rect, filename=image_path)
    doc.save(pdf_path)
    doc.close()
    img.close()
    logger.info(f'图片转PDF: {os.path.basename(image_path)}')
    return pdf_path


def _word_to_pdf(word_path, pdf_path):
    """将Word文档转换为PDF (使用MS Word COM自动化)"""
    com_error = None
    # 方法1: 尝试使用 pywin32 (MS Word)
    try:
        return _word_to_pdf_com(word_path, pdf_path)
    except Exception as e1:
        com_error = str(e1)
        logger.warning(f'Word COM转换失败: {com_error}, 尝试备用方案...')

    # 方法2: 尝试使用 LibreOffice
    try:
        return _convert_with_libreoffice(word_path, pdf_path)
    except Exception as e2:
        logger.error(f'Word转PDF全部失败: COM={com_error}, LibreOffice={e2}')
        return None


def _excel_to_pdf(excel_path, pdf_path):
    """将Excel文档转换为PDF (使用MS Excel COM自动化)"""
    com_error = None
    # 方法1: 尝试使用 pywin32 (MS Excel)
    try:
        return _excel_to_pdf_com(excel_path, pdf_path)
    except Exception as e1:
        com_error = str(e1)
        logger.warning(f'Excel COM转换失败: {com_error}, 尝试备用方案...')

    # 方法2: 尝试使用 LibreOffice
    try:
        return _convert_with_libreoffice(excel_path, pdf_path)
    except Exception as e2:
        logger.error(f'Excel转PDF全部失败: COM={com_error}, LibreOffice={e2}')
        return None


def _word_to_pdf_com(word_path, pdf_path):
    """使用 pywin32 + MS Word COM 转换"""
    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()
    word_app = None
    doc = None
    try:
        word_app = win32com.client.Dispatch('Word.Application')
        word_app.Visible = False
        word_app.DisplayAlerts = False

        doc = word_app.Documents.Open(
            os.path.abspath(word_path),
            ReadOnly=True,
            AddToRecentFiles=False
        )
        # 17 = wdFormatPDF
        doc.SaveAs(os.path.abspath(pdf_path), FileFormat=17)
        logger.info(f'Word转PDF(COM): {os.path.basename(word_path)}')
        return pdf_path
    finally:
        try:
            if doc:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word_app:
                word_app.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _excel_to_pdf_com(excel_path, pdf_path):
    """使用 pywin32 + MS Excel COM 转换"""
    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()
    excel_app = None
    wb = None
    try:
        excel_app = win32com.client.Dispatch('Excel.Application')
        excel_app.Visible = False
        excel_app.DisplayAlerts = False

        wb = excel_app.Workbooks.Open(
            os.path.abspath(excel_path),
            ReadOnly=True
        )
        # 0 = xlTypePDF
        wb.ExportAsFixedFormat(0, os.path.abspath(pdf_path))
        logger.info(f'Excel转PDF(COM): {os.path.basename(excel_path)}')
        return pdf_path
    finally:
        try:
            if wb:
                wb.Close(False)
        except Exception:
            pass
        try:
            if excel_app:
                excel_app.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _convert_with_libreoffice(source_path, pdf_path):
    """使用 LibreOffice 命令行转换"""
    soffice_paths = [
        r'C:\Program Files\LibreOffice\program\soffice.exe',
        r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
    ]

    soffice = None
    for p in soffice_paths:
        if os.path.isfile(p):
            soffice = p
            break

    if not soffice:
        raise FileNotFoundError('LibreOffice 未安装')

    output_dir = os.path.dirname(pdf_path)
    result = subprocess.run(
        [soffice, '--headless', '--convert-to', 'pdf',
         '--outdir', output_dir, source_path],
        capture_output=True, timeout=60
    )

    if result.returncode != 0:
        raise RuntimeError(f'LibreOffice转换失败: {result.stderr.decode("utf-8", errors="replace")}')

    # LibreOffice 输出文件名与源文件同名
    source_basename = os.path.splitext(os.path.basename(source_path))[0]
    generated_pdf = os.path.join(output_dir, f'{source_basename}.pdf')

    if generated_pdf != pdf_path and os.path.exists(generated_pdf):
        import shutil
        shutil.move(generated_pdf, pdf_path)

    logger.info(f'LibreOffice转PDF: {os.path.basename(source_path)}')
    return pdf_path


def check_conversion_capability():
    """检查系统是否支持Word/Excel转PDF"""
    capabilities = {
        'image': True,
        'pdf': True,
        'word': False,
        'excel': False,
        'method': None,
    }

    # 检查 pywin32
    try:
        import win32com.client
        capabilities['word'] = True
        capabilities['excel'] = True
        capabilities['method'] = 'MS Office (COM)'
    except ImportError:
        pass

    # 检查 LibreOffice
    if not capabilities['word']:
        for p in [r'C:\Program Files\LibreOffice\program\soffice.exe',
                   r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']:
            if os.path.isfile(p):
                capabilities['word'] = True
                capabilities['excel'] = True
                capabilities['method'] = 'LibreOffice'
                break

    return capabilities
