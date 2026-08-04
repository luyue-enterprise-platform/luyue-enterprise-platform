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


def _normalize_path(path):
    """
    规范化文件路径，处理Windows长路径问题
    """
    path = os.path.normpath(path)
    if os.name == 'nt':
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if len(path) > 255 and not path.startswith('\\\\?\\'):
            path = '\\\\?\\' + path
    return path


def convert_to_pdf(file_path, output_dir):
    """
    将任意支持的文件格式转换为PDF

    Args:
        file_path: 源文件路径
        output_dir: PDF输出目录

    Returns:
        str: 生成的PDF文件路径, 失败返回None
    """
    # 规范化路径
    file_path = _normalize_path(file_path)

    if not os.path.isfile(file_path):
        logger.error(f'文件不存在: {file_path}')
        return None

    ext = os.path.splitext(file_path)[1].lower()
    basename = os.path.splitext(os.path.basename(file_path))[0]
    pdf_path = os.path.join(output_dir, f'{basename}.pdf')

    # 避免文件名冲突（不同子文件夹中同名文件）
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
        logger.error(f'转换失败 [{ext}] {file_path}: {e}')
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
    """将Word文档转换为PDF (降级链: MS Word COM → WPS COM → LibreOffice)"""
    errors = []
    # 方法1: 尝试使用 MS Word COM
    try:
        return _word_to_pdf_com(word_path, pdf_path)
    except Exception as e1:
        errors.append(f'MS Word: {e1}')
        logger.warning(f'Word MS COM转换失败: {e1}, 尝试WPS...')

    # 方法2: 尝试使用 WPS Writer COM
    try:
        return _word_to_pdf_wps(word_path, pdf_path)
    except Exception as e2:
        errors.append(f'WPS: {e2}')
        logger.warning(f'Word WPS COM转换失败: {e2}, 尝试LibreOffice...')

    # 方法3: 尝试使用 LibreOffice
    try:
        return _convert_with_libreoffice(word_path, pdf_path)
    except Exception as e3:
        errors.append(f'LibreOffice: {e3}')
        logger.error(f'Word转PDF全部失败: {"; ".join(errors)}')
        return None


def _excel_to_pdf(excel_path, pdf_path):
    """将Excel文档转换为PDF (降级链: MS Excel COM → WPS COM → LibreOffice)"""
    errors = []
    # 方法1: 尝试使用 MS Excel COM
    try:
        return _excel_to_pdf_com(excel_path, pdf_path)
    except Exception as e1:
        errors.append(f'MS Excel: {e1}')
        logger.warning(f'Excel MS COM转换失败: {e1}, 尝试WPS...')

    # 方法2: 尝试使用 WPS Spreadsheet COM
    try:
        return _excel_to_pdf_wps(excel_path, pdf_path)
    except Exception as e2:
        errors.append(f'WPS: {e2}')
        logger.warning(f'Excel WPS COM转换失败: {e2}, 尝试LibreOffice...')

    # 方法3: 尝试使用 LibreOffice
    try:
        return _convert_with_libreoffice(excel_path, pdf_path)
    except Exception as e3:
        errors.append(f'LibreOffice: {e3}')
        logger.error(f'Excel转PDF全部失败: {"; ".join(errors)}')
        return None


def _word_to_pdf_com(word_path, pdf_path):
    """使用 pywin32 + MS Word COM 转换"""
    import win32com.client
    import pythoncom

    # 规范化路径（COM要求绝对路径，反斜杠分隔）
    word_abs = os.path.abspath(word_path)
    pdf_abs = os.path.abspath(pdf_path)

    pythoncom.CoInitialize()
    word_app = None
    doc = None
    try:
        word_app = win32com.client.Dispatch('Word.Application')
        word_app.Visible = False
        word_app.DisplayAlerts = False

        doc = word_app.Documents.Open(
            word_abs,
            ReadOnly=True,
            AddToRecentFiles=False
        )
        # 17 = wdFormatPDF
        doc.SaveAs(pdf_abs, FileFormat=17)
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

    # 规范化路径（COM要求绝对路径，反斜杠分隔）
    excel_abs = os.path.abspath(excel_path)
    pdf_abs = os.path.abspath(pdf_path)

    pythoncom.CoInitialize()
    excel_app = None
    wb = None
    try:
        excel_app = win32com.client.Dispatch('Excel.Application')
        excel_app.Visible = False
        excel_app.DisplayAlerts = False

        wb = excel_app.Workbooks.Open(
            excel_abs,
            ReadOnly=True
        )
        # 0 = xlTypePDF
        wb.ExportAsFixedFormat(0, pdf_abs)
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


def _word_to_pdf_wps(word_path, pdf_path):
    """使用 WPS Writer COM (KWps.Application) 转换Word为PDF"""
    import win32com.client
    import pythoncom

    word_abs = os.path.abspath(word_path)
    pdf_abs = os.path.abspath(pdf_path)

    pythoncom.CoInitialize()
    wps_app = None
    doc = None
    try:
        wps_app = win32com.client.Dispatch('KWps.Application')
        wps_app.Visible = False
        wps_app.DisplayAlerts = False

        doc = wps_app.Documents.Open(
            word_abs,
            ReadOnly=True,
            AddToRecentFiles=False
        )
        # WPS Writer 导出PDF：优先用 ExportAsFixedFormat，备选 SaveAs
        try:
            # wdExportFormatPDF = 17
            doc.ExportAsFixedFormat(pdf_abs, 17)
        except Exception:
            # 备选: SaveAs with FileFormat=17 (wdFormatPDF)
            doc.SaveAs2(pdf_abs, FileFormat=17)
        logger.info(f'Word转PDF(WPS): {os.path.basename(word_path)}')
        return pdf_path
    finally:
        try:
            if doc:
                doc.Close(False)
        except Exception:
            pass
        try:
            if wps_app:
                wps_app.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _excel_to_pdf_wps(excel_path, pdf_path):
    """使用 WPS Spreadsheet COM (Ket.Application) 转换Excel为PDF"""
    import win32com.client
    import pythoncom

    excel_abs = os.path.abspath(excel_path)
    pdf_abs = os.path.abspath(pdf_path)

    pythoncom.CoInitialize()
    et_app = None
    wb = None
    try:
        et_app = win32com.client.Dispatch('Ket.Application')
        et_app.Visible = False
        et_app.DisplayAlerts = False

        wb = et_app.Workbooks.Open(
            excel_abs,
            ReadOnly=True
        )
        # 0 = xlTypePDF
        wb.ExportAsFixedFormat(0, pdf_abs)
        logger.info(f'Excel转PDF(WPS): {os.path.basename(excel_path)}')
        return pdf_path
    finally:
        try:
            if wb:
                wb.Close(False)
        except Exception:
            pass
        try:
            if et_app:
                et_app.Quit()
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

    methods = []

    # 检查 MS Office COM (pywin32 + Word.Application/Excel.Application)
    try:
        import win32com.client
        try:
            import pythoncom
            pythoncom.CoInitialize()
            try:
                test = win32com.client.Dispatch('Word.Application')
                test.Quit()
                methods.append('MS Office')
                capabilities['word'] = True
                capabilities['excel'] = True
            except Exception:
                pass
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    except ImportError:
        pass

    # 检查 WPS Office COM (KWps.Application / Ket.Application)
    if not capabilities['word']:
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            try:
                test = win32com.client.Dispatch('KWps.Application')
                test.Quit()
                methods.append('WPS Office')
                capabilities['word'] = True
                capabilities['excel'] = True
            except Exception:
                pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        except ImportError:
            pass

    # 检查 LibreOffice
    if not capabilities['word']:
        for p in [r'C:\Program Files\LibreOffice\program\soffice.exe',
                   r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']:
            if os.path.isfile(p):
                methods.append('LibreOffice')
                capabilities['word'] = True
                capabilities['excel'] = True
                break

    capabilities['method'] = ' + '.join(methods) if methods else None
    return capabilities
