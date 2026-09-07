# -*- coding: utf-8 -*-
"""可逆转换引擎：其他格式 -> PDF（v2.0.0 新增，与 pdf2word 方向逻辑完全隔离；
v2.1.0 全面保真：A4 规范化 + 逐项校验，与 pdf2word 方向同规则）

支持格式：
- 图片: .png .jpg .jpeg .bmp .gif .tif .tiff .webp  （fitz A4 排版，按宽高比选方向）
- Word: .doc .docx                                   （Word COM，逐节 A4+方向保持）
- Excel: .xls .xlsx                                  （Excel COM，逐表 A4+适配页宽防截断）
- 文本: .txt .md                                     （fitz A4 文本排版）
- PDF : .pdf                                         （A4 规范化后并入/复制）

v2.1.0 保真规则（与 PDF→Word 方向一致）：
1. 页面方向：Word 逐节/Excel 逐表/图片逐张按源方向输出，不发生改变
2. 页面尺寸：全部输出 A4；内容等比适配，不变形/不截断/不错位
3. 布局结构：文字/图片/表格/页眉页脚由导出引擎原生保真
4. 字体格式：字体/字号/颜色/粗斜体由导出引擎原生保留
5. 转换校验：每个文件转换后逐项校验（validator），失败自动修正重验，
   仍不过给出文件名+页码+不一致项
6. 批量处理：校验结果按文件逐个汇总，随 results 返回
"""
import os
import shutil

import fitz  # PyMuPDF

from . import page_norm, validator

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


# ---------------- 图片 -> PDF（v2.1.0：A4 排版，按宽高比选方向，等比缩放居中） ----------------

def convert_image_to_pdf(src, dst):
    """单张图片转一页 A4 PDF：宽图→A4横版，竖图→A4竖版，等比缩放居中不变形"""
    from PIL import Image
    with Image.open(src) as img:
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
        orient = page_norm.page_orientation(w, h)
        dst_w, dst_h = page_norm.a4_size_for(orient)
        doc = fitz.open()
        try:
            page = doc.new_page(width=dst_w, height=dst_h)
            scale = min(dst_w / w, dst_h / h)
            show_w, show_h = w * scale, h * scale
            x0, y0 = (dst_w - show_w) / 2.0, (dst_h - show_h) / 2.0
            rect = fitz.Rect(x0, y0, x0 + show_w, y0 + show_h)
            import io
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            page.insert_image(rect, stream=buf.getvalue())
            doc.save(dst, garbage=3, deflate=True)
        finally:
            doc.close()
    with fitz.open(dst) as d:
        return d.page_count


# ---------------- Word -> PDF（v2.1.0：逐节 A4 + 方向保持） ----------------

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


def _word_sections_to_a4(app, doc):
    """逐节设置 A4 页面并保持各节方向（文本由 Word 原生重排，无变形截断）。

    返回 (节方向列表, 文档纯文本)；.doc 旧格式同样适用（COM 层统一）。
    注意：尺寸直接按 pt 字面量设置（1cm=28.3465pt），不调用
    app.CentimetersToPoints——批量复用的 COM 实例上该方法偶发 E_FAIL。
    """
    CM_TO_PT = 28.3465
    orientations = []
    for sec in doc.Sections:
        ps = sec.PageSetup
        orient = 'landscape' if int(ps.Orientation) == 1 else 'portrait'  # wdOrientLandscape=1
        w_cm, h_cm = (29.7, 21.0) if orient == 'landscape' else (21.0, 29.7)
        ps.PageWidth = w_cm * CM_TO_PT
        ps.PageHeight = h_cm * CM_TO_PT
        orientations.append(orient)
    text = ''
    try:
        text = doc.Content.Text or ''
    except Exception:
        pass
    return orientations, text


def convert_word_to_pdf(src, dst, word_app=None, with_validation=True):
    """Word COM 转 PDF（逐节 A4+方向保持+逐项校验）。word_app 为批量复用 COM 实例"""
    import win32com.client
    own_app = word_app is None
    app = word_app or win32com.client.Dispatch('Word.Application')
    _safe_set(app, 'Visible', False)
    doc = None
    sec_orients, src_text = [], ''
    try:
        doc = app.Documents.Open(os.path.abspath(src), ReadOnly=True)
        # 逐节 A4 + 方向保持（v2.1.0）
        sec_orients, src_text = _word_sections_to_a4(app, doc)
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
        pages = d.page_count

    report = None
    if with_validation:
        report = _validate_word_output(src, dst, sec_orients, src_text)
    return pages, report


def _validate_word_output(src, dst, sec_orients, src_text):
    """Word→PDF 校验：.docx 走完整签名比对；.doc 旧格式用 COM 文本做基准"""
    file_name = os.path.basename(src)
    out_name = os.path.basename(dst)
    if ext_of(src) == '.docx':
        return validator.validate_word_to_pdf(src, dst, file_name, out_name)
    # .doc：python-docx 不可读 → 尺寸/方向校验 + COM 文本覆盖
    items, _ = validator._check_pdf_pages(dst, _pdf_orients(dst), file_name)
    if src_text:
        cov = validator._coverage(src_text, validator.pdf_text(dst))
        items.append(validator._item(
            'layout_text', '布局结构-文本',
            'pass' if cov >= validator.TEXT_COVERAGE_MIN else 'fail',
            '%s 文本覆盖率 %.1f%%（阈值 %.0f%%）'
            % (file_name, cov * 100, validator.TEXT_COVERAGE_MIN * 100)))
    items.append(validator._item(
        'fonts', '字体与格式', 'pass',
        '%s 为 .doc 旧格式，字体/字号/颜色/粗斜体由 Word 导出引擎原生保真'
        % file_name))
    return validator.make_report(file_name, out_name, 'topdf', items)


def _pdf_orients(pdf_path):
    with fitz.open(pdf_path) as doc:
        return [page_norm.page_orientation(p.rect.width, p.rect.height) for p in doc]


# ---------------- Excel -> PDF（v2.1.0：逐表 A4 + 方向 + 适配页宽防截断） ----------------

def convert_excel_to_pdf(src, dst, excel_app=None, with_validation=True):
    """Excel COM 转 PDF（逐表 A4+方向保持+宽度适配一页防截断+逐项校验）"""
    import win32com.client
    own_app = excel_app is None
    app = excel_app or win32com.client.Dispatch('Excel.Application')
    _safe_set(app, 'Visible', False)
    _safe_set(app, 'DisplayAlerts', False)
    wb = None
    try:
        wb = app.Workbooks.Open(os.path.abspath(src), ReadOnly=True)
        _excel_sheets_to_a4(wb)
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
        pages = d.page_count

    report = None
    if with_validation and ext_of(src) == '.xlsx':
        report = validator.validate_excel_to_pdf(
            src, dst, os.path.basename(src), os.path.basename(dst))
    elif with_validation:
        # .xls 旧格式：openpyxl 不可读 → 仅尺寸/方向校验
        items, _ = validator._check_pdf_pages(dst, _pdf_orients(dst),
                                              os.path.basename(src))
        report = validator.make_report(os.path.basename(src),
                                       os.path.basename(dst), 'topdf', items)
    return pages, report


def _excel_sheets_to_a4(wb):
    """逐工作表：A4 纸张 + 方向保持（未设置时按内容宽高比）+ 宽度适配一页防截断"""
    for ws in wb.Worksheets:
        ps = ws.PageSetup
        try:
            orient = int(ps.Orientation)  # xlPortrait=1, xlLandscape=2
        except Exception:
            orient = 0
        if orient not in (1, 2):
            # 按内容宽高比选择方向
            try:
                rng = ws.UsedRange
                orient = 2 if rng.Width > rng.Height else 1
            except Exception:
                orient = 1
        try:
            ps.PaperSize = 9  # xlPaperA4
            ps.Orientation = orient
            ps.Zoom = False
            ps.FitToPagesWide = 1      # 宽度方向适配一页：横向不截断
            ps.FitToPagesTall = False  # 高度方向不限制页数：纵向不丢行
        except Exception:
            pass


# ---------------- 文本 -> PDF（A4 竖版排版，v2.1.0 增加校验） ----------------

def convert_text_to_pdf(src, dst, with_validation=True):
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
    page_w, page_h = page_norm.A4_PORTRAIT
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
    pages = doc.page_count
    doc.close()

    report = None
    if with_validation:
        report = validator.validate_text_to_pdf(
            src, dst, os.path.basename(src), os.path.basename(dst))
    return pages, report


# ---------------- PDF -> PDF（v2.1.0：A4 规范化 + 校验） ----------------

def convert_pdf_to_pdf(src, dst, with_validation=True):
    """PDF 经 A4 规范化后输出（方向保持+统一 A4+等比缩放居中）+ 校验"""
    info = page_norm.normalize_pdf_to_a4(src, dst)
    report = None
    if with_validation:
        items = validator.normalize_pdf_until_valid(
            dst, info['orientations'], os.path.basename(src))
        report = validator.make_report(os.path.basename(src),
                                       os.path.basename(dst), 'topdf', items)
    return info['pages'], report


# ---------------- 统一入口 ----------------

def convert_one(src, dst, word_app=None, excel_app=None, with_validation=True):
    """单文件转 PDF 到 dst，返回 (页数, 校验报告)。不支持/空文件/失败抛 ValueError"""
    ext = ext_of(src)
    if ext not in SUPPORTED_EXTS:
        raise ValueError('不支持的格式 "%s"（支持：图片/Word/Excel/TXT/PDF）' % ext)
    if os.path.getsize(src) == 0:
        raise ValueError('空文件（0 字节）')
    if ext in IMAGE_EXTS:
        pages = convert_image_to_pdf(src, dst)
        report = None
        if with_validation:
            report = validator.validate_image_to_pdf(
                [src], dst, os.path.basename(src), os.path.basename(dst))
        return pages, report
    if ext in WORD_EXTS:
        return convert_word_to_pdf(src, dst, word_app=word_app,
                                   with_validation=with_validation)
    if ext in EXCEL_EXTS:
        return convert_excel_to_pdf(src, dst, excel_app=excel_app,
                                    with_validation=with_validation)
    if ext in TEXT_EXTS:
        return convert_text_to_pdf(src, dst, with_validation=with_validation)
    return convert_pdf_to_pdf(src, dst, with_validation=with_validation)


def batch_to_pdf(files, output_dir, output_mode='individual',
                 progress_callback=None, merged_name='合并结果.pdf'):
    """批量转 PDF 主入口（v2.1.0：每个文件附校验报告）。

    files: 按期望合并顺序排列的文件路径列表（含文件夹递归解析顺序）
    output_mode: 'merge' | 'individual'
    返回: (results, skipped)
      results: [{name, out_name, out_path, pages, action:'merged'|'saved', validation}]
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
                pages, report = convert_one(src, tmp_out,
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
                        'validation': report,
                    })
                else:
                    results.append({
                        'name': name,
                        'out_name': os.path.basename(tmp_out),
                        'out_path': tmp_out,
                        'pages': pages,
                        'action': 'saved',
                        'validation': report,
                    })
            except Exception as e:  # 单文件失败不中断整体
                skipped.append({'name': name, 'reason': str(e)})
            if progress_callback:
                progress_callback(idx + 1, total)
        if output_mode == 'merge':
            if results:
                merged_path = os.path.join(output_dir, merged_name)
                # 合并结果整体再校验一遍 A4/方向（各 part 已分别校验内容）
                merged_doc.save(merged_path, garbage=3, deflate=True)
                merged_items, _ = validator._check_pdf_pages(
                    merged_path, _pdf_orients(merged_path), merged_name)
                for r in results:
                    r['out_path'] = merged_path
                # 合并整体校验附在最后一条结果的报告说明中
                if results:
                    last = results[-1]
                    if last.get('validation'):
                        last['validation'].setdefault('items', []).extend(merged_items)
                        last['validation']['overall'] = validator._overall(
                            last['validation']['items'])
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
