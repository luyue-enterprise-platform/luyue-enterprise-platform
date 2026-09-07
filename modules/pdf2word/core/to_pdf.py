# -*- coding: utf-8 -*-
"""可逆转换引擎：其他格式 -> PDF（v2.0.0 新增，与 pdf2word 方向逻辑完全隔离）

支持格式：
- 图片: .png .jpg .jpeg .bmp .gif .tif .tiff .webp  （PIL）
- Word: .doc .docx                                   （Word COM，wdFormatPDF=17）
- Excel: .xls .xlsx                                  （Excel COM，xlTypePDF=57）
- 文本: .txt .md                                     （fitz 文本排版）
- PDF : .pdf                                         （原样并入/复制）

输出模式：
- merge      合并模式：按传入顺序把全部文件合并为单个 PDF
- individual 独立模式：每个源文件生成独立 PDF，命名与源一致（仅换扩展名，
             重名自动加序号），一一对应可溯源

健壮性：
- 不支持格式 / 空文件 / 转换失败均返回结构化错误（含文件名与原因），不抛中断
"""
import os
import shutil

import fitz  # PyMuPDF

# ---------------- 支持格式 ----------------
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif', '.tiff', '.webp'}
WORD_EXTS = {'.doc', '.docx'}
EXCEL_EXTS = {'.xls', '.xlsx'}
TEXT_EXTS = {'.txt', '.md'}
PDF_EXTS = {'.pdf'}
SUPPORTED_EXTS = IMAGE_EXTS | WORD_EXTS | EXCEL_EXTS | TEXT_EXTS | PDF_EXTS


def ext_of(path):
    return os.path.splitext(path)[1].lower()


def is_supported(path):
    return ext_of(path) in SUPPORTED_EXTS


def _unique_out_path(out_dir, base_name, used_names):
    """独立模式命名：与源文件同名仅换扩展名；重名自动加序号"""
    name = base_name + '.pdf'
    n = 1
    while name in used_names or os.path.exists(os.path.join(out_dir, name)):
        name = '%s(%d).pdf' % (base_name, n)
        n += 1
    used_names.add(name)
    return os.path.join(out_dir, name)


def convert_image_to_pdf(src, dst):
    from PIL import Image
    img = Image.open(src)
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')
    img.save(dst, 'PDF', resolution=100.0)
    with fitz.open(dst) as d:
        return d.page_count


def _safe_set(app, prop, value):
    """COM 动态派发下部分属性不可写时静默跳过（不影响转换结果）"""
    try:
        setattr(app, prop, value)
    except Exception:
        pass


def _safe_quit(app):
    """关闭 COM 应用实例；失败（含动态派发 AttributeError）静默跳过"""
    try:
        app.Quit()
    except Exception:
        pass


def convert_word_to_pdf(src, dst, word_app=None):
    """Word COM 转 PDF。word_app 为批量复用的 COM 实例，None 则自建"""
    import win32com.client
    own_app = word_app is None
    app = word_app or win32com.client.Dispatch('Word.Application')
    _safe_set(app, 'Visible', False)
    doc = None
    try:
        doc = app.Documents.Open(os.path.abspath(src), ReadOnly=True)
        doc.SaveAs2(os.path.abspath(dst), FileFormat=17)  # wdFormatPDF
    finally:
        if doc is not None:
            _safe_set(doc, 'Saved', True)
            try:
                doc.Close(False)
            except Exception:
                pass
        if own_app:
            _safe_quit(app)
    with fitz.open(dst) as d:
        return d.page_count


def convert_excel_to_pdf(src, dst, excel_app=None):
    """Excel COM 转 PDF。excel_app 为批量复用的 COM 实例，None 则自建"""
    import win32com.client
    own_app = excel_app is None
    app = excel_app or win32com.client.Dispatch('Excel.Application')
    _safe_set(app, 'Visible', False)
    _safe_set(app, 'DisplayAlerts', False)
    wb = None
    try:
        wb = app.Workbooks.Open(os.path.abspath(src), ReadOnly=True)
        # 首选 ExportAsFixedFormat（命名参数）；部分环境该组件异常 → 回退 SaveAs
        try:
            wb.ExportAsFixedFormat(Type=57, Filename=os.path.abspath(dst),
                                   Quality=0, IncludeDocProperties=True,
                                   IgnorePrintAreas=False, OpenAfterPublish=False)
        except Exception:
            wb.SaveAs(Filename=os.path.abspath(dst), FileFormat=57)
    finally:
        if wb is not None:
            try:
                wb.Close(False)
            except Exception:
                pass
        if own_app:
            _safe_quit(app)
    with fitz.open(dst) as d:
        return d.page_count


def convert_text_to_pdf(src, dst):
    """文本文件排版为 A4 PDF（自动换行分页）"""
    raw = None
    for enc in ('utf-8', 'gbk', 'utf-16'):
        try:
            with open(src, 'r', encoding=enc) as f:
                raw = f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if raw is None:
        raise ValueError('文本编码无法识别（已尝试 utf-8/gbk/utf-16）')
    doc = fitz.open()
    page_w, page_h = fitz.paper_size('a4')
    margin, font_size, line_h = 56, 11, 16
    chars_per_line = int((page_w - 2 * margin) / (font_size * 0.62))
    lines = []
    for para in raw.splitlines():
        if not para:
            lines.append('')
        else:
            while len(para) > chars_per_line:
                lines.append(para[:chars_per_line])
                para = para[chars_per_line:]
            lines.append(para)
    per_page = int((page_h - 2 * margin) / line_h)
    doc = fitz.open()
    for i in range(0, max(len(lines), 1), per_page):
        page = doc.new_page(width=page_w, height=page_h)
        y = margin
        for ln in lines[i:i + per_page]:
            # 中文字体回退：使用内置 CJK 字体
            page.insert_text((margin, y), ln, fontsize=font_size,
                             fontname='china-s')
            y += line_h
    doc.save(dst)
    return doc.page_count


def convert_pdf_to_pdf(src, dst):
    """PDF 原样复制（独立模式）或返回源路径供合并"""
    shutil.copy2(src, dst)
    with fitz.open(dst) as d:
        return d.page_count


def convert_one(src, dst, word_app=None, excel_app=None):
    """单文件转 PDF 到 dst，返回页数。不支持/空文件/失败抛 ValueError"""
    ext = ext_of(src)
    if ext not in SUPPORTED_EXTS:
        raise ValueError('不支持的格式 "%s"（支持：图片/Word/Excel/TXT/PDF）' % ext)
    if os.path.getsize(src) == 0:
        raise ValueError('空文件（0 字节）')
    if ext in IMAGE_EXTS:
        return convert_image_to_pdf(src, dst)
    if ext in WORD_EXTS:
        return convert_word_to_pdf(src, dst, word_app=word_app)
    if ext in EXCEL_EXTS:
        return convert_excel_to_pdf(src, dst, excel_app=excel_app)
    if ext in TEXT_EXTS:
        return convert_text_to_pdf(src, dst)
    return convert_pdf_to_pdf(src, dst)


def batch_to_pdf(files, output_dir, output_mode='individual',
                 progress_callback=None, merged_name='合并结果.pdf'):
    """批量转 PDF 主入口。

    files: 按期望合并顺序排列的文件路径列表（含文件夹递归解析顺序）
    output_mode: 'merge' | 'individual'
    返回: (results, skipped)
      results: [{name, out_name, out_path, pages, action:'merged'|'saved'}]
      skipped: [{name, reason}]
    """
    os.makedirs(output_dir, exist_ok=True)
    results, skipped = [], []
    used_names = set()
    tmp_dir = os.path.join(output_dir, '_tmp_topdf')
    if output_mode == 'merge':
        os.makedirs(tmp_dir, exist_ok=True)
    merged_doc = fitz.open() if output_mode == 'merge' else None

    # 批量复用 COM 实例（惰性创建，线程内初始化）
    word_app = excel_app = None
    com_inited = False
    total = len(files)
    try:
        for idx, src in enumerate(files):
            name = os.path.basename(src)
            try:
                if not com_inited and ext_of(src) in (WORD_EXTS | EXCEL_EXTS):
                    import pythoncom
                    pythoncom.CoInitialize()
                    com_inited = True
                if output_mode == 'merge':
                    tmp_out = os.path.join(tmp_dir, 'part_%05d.pdf' % idx)
                else:
                    base = os.path.splitext(name)[0]
                    tmp_out = _unique_out_path(output_dir, base, used_names)
                if ext_of(src) in WORD_EXTS and word_app is None:
                    import win32com.client
                    word_app = win32com.client.Dispatch('Word.Application')
                    _safe_set(word_app, 'Visible', False)
                if ext_of(src) in EXCEL_EXTS and excel_app is None:
                    import win32com.client
                    excel_app = win32com.client.Dispatch('Excel.Application')
                    _safe_set(excel_app, 'Visible', False)
                    _safe_set(excel_app, 'DisplayAlerts', False)
                pages = convert_one(src, tmp_out,
                                    word_app=word_app, excel_app=excel_app)
                if output_mode == 'merge':
                    with fitz.open(tmp_out) as part:
                        merged_doc.insert_pdf(part)
                    results.append({
                        'name': name,
                        'out_name': merged_name,
                        'out_path': None,
                        'pages': pages,
                        'action': 'merged',
                    })
                else:
                    results.append({
                        'name': name,
                        'out_name': os.path.basename(tmp_out),
                        'out_path': tmp_out,
                        'pages': pages,
                        'action': 'saved',
                    })
            except Exception as e:  # 单文件失败不中断整体
                skipped.append({'name': name, 'reason': str(e)})
            if progress_callback:
                progress_callback(idx + 1, total)
        if output_mode == 'merge':
            if results:
                merged_path = os.path.join(output_dir, merged_name)
                merged_doc.save(merged_path, garbage=3, deflate=True)
                for r in results:
                    r['out_path'] = merged_path
    finally:
        if merged_doc is not None:
            merged_doc.close()
        if word_app is not None:
            _safe_quit(word_app)
        if excel_app is not None:
            _safe_quit(excel_app)
        if com_inited:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return results, skipped
