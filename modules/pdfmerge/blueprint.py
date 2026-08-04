# -*- coding: utf-8 -*-
"""汇总智能系统 — Flask Blueprint

API 流程：
1. 选择文件/文件夹 → 获取文件路径
2. 处理（转换图片 + OCR + 智能匹配）→ 后台任务 + 进度轮询
3. 生成 PDF（封面 + 目录 + 图片页 + 页码）→ 后台任务
4. 预览 / 下载 / 另存为
"""

import os
import sys
import uuid
import logging
import threading
import traceback

from flask import (
    Blueprint, render_template, request, jsonify,
    send_file, Response
)

from core.auth import login_required

# ===== 路径设置 =====
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'pdfmerge')
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 图片临时目录
IMAGE_TEMP_DIR = os.path.join(DATA_DIR, 'pdfmerge_images')
PDF_OUTPUT_DIR = os.path.join(DATA_DIR, 'pdfmerge_output')

for d in [IMAGE_TEMP_DIR, PDF_OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)

# ===== Blueprint =====
pdfmerge_bp = Blueprint(
    'pdfmerge',
    __name__,
    url_prefix='/pdfmerge',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static')
)

logger = logging.getLogger('pdfmerge')

# ===== 全局状态 =====
_tasks = {}
_tasks_lock = threading.Lock()

# 图片池: {image_id: {id, path, original_filename, ocr_text, source_ext, page_num}}
_image_pool = {}
_image_pool_lock = threading.Lock()

# 已处理的源文件路径（避免重复转换/OCR）
_processed_files = set()


# ===== 路由 =====

@pdfmerge_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('pdfmerge_index.html')


# ===== 文件选择 =====

@pdfmerge_bp.route('/api/select_folder', methods=['POST'])
@login_required
def api_select_folder():
    """弹出系统原生文件夹选择对话框，递归扫描文件"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        folder_path = filedialog.askdirectory(
            title='选择资料文件夹',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件夹选择异常: {e}')
        return jsonify({'error': str(e)}), 500

    if not folder_path:
        return jsonify({'cancelled': True})

    if not os.path.isdir(folder_path):
        return jsonify({'error': '选择的路径不是文件夹'}), 400

    from .core.image_converter import scan_folder_recursive, SUPPORTED_EXTS

    all_files = scan_folder_recursive(folder_path)

    file_list = []
    for fp in all_files:
        file_list.append({
            'name': os.path.basename(fp),
            'abs_path': fp,
            'ext': os.path.splitext(fp)[1].lower(),
            'size': os.path.getsize(fp),
        })

    logger.info(f'文件夹扫描: {folder_path} -> {len(file_list)} 个文件')

    return jsonify({
        'ok': True,
        'folder_path': folder_path,
        'file_count': len(file_list),
        'files': file_list,
    })


@pdfmerge_bp.route('/api/select_files', methods=['POST'])
@login_required
def api_select_files():
    """弹出系统原生文件选择对话框（多选）"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        file_paths = filedialog.askopenfilenames(
            title='选择资料文件（可多选）',
            filetypes=[
                ('所有支持的文件', '*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.doc *.docx *.xls *.xlsx'),
                ('PDF文件', '*.pdf'),
                ('图片文件', '*.png *.jpg *.jpeg *.bmp *.tif *.tiff'),
                ('Word文件', '*.doc *.docx'),
                ('Excel文件', '*.xls *.xlsx'),
            ],
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件选择异常: {e}')
        return jsonify({'error': str(e)}), 500

    if not file_paths:
        return jsonify({'cancelled': True})

    from .core.image_converter import SUPPORTED_EXTS

    file_list = []
    for fp in file_paths:
        ext = os.path.splitext(fp)[1].lower()
        if ext not in SUPPORTED_EXTS:
            continue
        file_list.append({
            'name': os.path.basename(fp),
            'abs_path': fp,
            'ext': ext,
            'size': os.path.getsize(fp),
        })

    logger.info(f'文件选择: {len(file_list)} 个文件')

    return jsonify({
        'ok': True,
        'file_count': len(file_list),
        'files': file_list,
    })


# ===== 处理（转换 + OCR + 匹配）=====

@pdfmerge_bp.route('/api/process', methods=['POST'])
@login_required
def api_process():
    """启动文件处理任务：转换图片 → OCR → 智能匹配"""
    data = request.get_json(silent=True) or {}
    file_paths = data.get('file_paths', [])
    mode = data.get('mode', 'refund')

    if not file_paths:
        return jsonify({'error': '未提供文件路径'}), 400

    # 过滤有效文件
    valid_paths = [fp for fp in file_paths if os.path.isfile(fp)]
    if not valid_paths:
        return jsonify({'error': '没有有效的文件'}), 400

    task_id = str(uuid.uuid4())[:8]

    with _tasks_lock:
        _tasks[task_id] = {
            'status': 'processing',
            'progress': 0,
            'message': '正在准备...',
            'result': None,
            'type': 'process',
        }

    # 启动后台任务
    thread = threading.Thread(
        target=_process_task,
        args=(task_id, valid_paths, mode),
        daemon=True
    )
    thread.start()

    return jsonify({'task_id': task_id})


def _process_task(task_id, file_paths, mode):
    """后台处理：转换图片 → OCR → 匹配"""
    try:
        from .core.image_converter import convert_to_images
        from .core.ocr_engine import ocr_image
        from .core.sections import match_images

        # 过滤已处理的文件
        new_files = [fp for fp in file_paths if fp not in _processed_files]
        if not new_files:
            # 所有文件已处理，直接重新匹配
            _update_task(task_id, progress=90, message='所有文件已处理，正在重新匹配...')
        else:
            new_files = [os.path.normpath(fp) for fp in new_files]

        total_files = len(new_files)
        all_new_images = []

        # === 第一步：转换文件为图片 ===
        for i, file_path in enumerate(new_files):
            _update_task(task_id, progress=int(i / max(total_files, 1) * 40),
                         message=f'转换文件 {i + 1}/{total_files}: {os.path.basename(file_path)}')

            images = convert_to_images(file_path, IMAGE_TEMP_DIR, file_index=i)
            for img_info in images:
                image_id = str(uuid.uuid4())[:12]
                img_data = {
                    'id': image_id,
                    'path': img_info['path'],
                    'original_filename': img_info['original_filename'],
                    'page_num': img_info.get('page_num', 1),
                    'source_ext': img_info.get('source_ext', ''),
                    'ocr_text': '',
                }
                all_new_images.append(img_data)

                with _image_pool_lock:
                    _image_pool[image_id] = img_data

            _processed_files.add(file_path)

        if not all_new_images:
            _update_task(task_id, status='error', message='没有文件成功转换为图片')
            return

        # === 第二步：OCR 识别 ===
        # 只 OCR 新增的图片
        total_ocr = len(all_new_images)
        for i, img_data in enumerate(all_new_images):
            _update_task(task_id,
                         progress=40 + int(i / total_ocr * 40),
                         message=f'OCR识别 {i + 1}/{total_ocr}: {img_data["original_filename"]}')

            text = ocr_image(img_data['path'])
            img_data['ocr_text'] = text

            with _image_pool_lock:
                _image_pool[img_data['id']]['ocr_text'] = text

        # === 第三步：智能匹配 ===
        _update_task(task_id, progress=85, message='正在智能匹配...')

        with _image_pool_lock:
            all_images = list(_image_pool.values())

        match_result = match_images(all_images, mode)

        _update_task(task_id, progress=100, status='done',
                     message=f'匹配完成: {len(all_images)} 张图片, {len(match_result["sections"])} 个章节',
                     result=match_result)

    except Exception as e:
        logger.error(f'处理任务失败: {e}', exc_info=True)
        _update_task(task_id, status='error', message=f'处理失败: {e}')


# ===== 重新匹配（不重新转换/OCR）=====

@pdfmerge_bp.route('/api/rematch', methods=['POST'])
@login_required
def api_rematch():
    """用新模式重新匹配现有图片池"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'refund')

    with _image_pool_lock:
        all_images = list(_image_pool.values())

    if not all_images:
        return jsonify({'error': '图片池为空，请先添加文件'}), 400

    from .core.sections import match_images

    match_result = match_images(all_images, mode)

    return jsonify(match_result)


# ===== PDF 生成 =====

@pdfmerge_bp.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    """启动 PDF 生成任务"""
    data = request.get_json(silent=True) or {}
    sections = data.get('sections', [])
    cover_info = data.get('cover_info', {})
    mode = data.get('mode', 'refund')

    if not sections:
        return jsonify({'error': '没有章节数据'}), 400

    # 过滤有图片的章节，并解析图片路径
    prepared_sections = []
    for s in sections:
        images = []
        for img in s.get('images', []):
            if isinstance(img, dict):
                img_id = img.get('id', '')
                with _image_pool_lock:
                    pool_img = _image_pool.get(img_id)
                if pool_img:
                    images.append(pool_img)
            elif isinstance(img, str):
                with _image_pool_lock:
                    pool_img = _image_pool.get(img)
                if pool_img:
                    images.append(pool_img)

        if images:
            prepared_sections.append({
                'id': s.get('id', ''),
                'name': s.get('name', ''),
                'year': s.get('year'),
                'images': images,
            })

    if not prepared_sections:
        return jsonify({'error': '没有匹配到可用的图片'}), 400

    task_id = str(uuid.uuid4())[:8]
    output_filename = f'汇总PDF_{task_id}.pdf'
    output_path = os.path.join(PDF_OUTPUT_DIR, output_filename)

    with _tasks_lock:
        _tasks[task_id] = {
            'status': 'processing',
            'progress': 0,
            'message': '正在生成PDF...',
            'result': None,
            'type': 'generate',
            'output_path': output_path,
        }

    thread = threading.Thread(
        target=_generate_task,
        args=(task_id, prepared_sections, output_path, cover_info, mode),
        daemon=True
    )
    thread.start()

    return jsonify({'task_id': task_id})


def _generate_task(task_id, sections, output_path, cover_info, mode):
    """后台 PDF 生成"""
    try:
        from .core.pdf_builder import build_pdf

        def progress_cb(current, total, msg):
            _update_task(task_id, progress=int(current / total * 100), message=msg)

        result = build_pdf(sections, output_path, cover_info, mode, progress_callback=progress_cb)

        _update_task(task_id, progress=100, status='done',
                     message=f'PDF生成完成，共 {result["page_count"]} 页',
                     result={
                         'page_count': result['page_count'],
                         'content_pages': result['content_pages'],
                         'output_path': output_path,
                         'filename': os.path.basename(output_path),
                     })

    except Exception as e:
        logger.error(f'PDF生成失败: {e}', exc_info=True)
        _update_task(task_id, status='error', message=f'生成失败: {e}')


# ===== 进度轮询 =====

@pdfmerge_bp.route('/api/progress/<task_id>')
@login_required
def api_progress(task_id):
    """轮询任务进度"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify({
        'status': task['status'],
        'progress': task['progress'],
        'message': task['message'],
        'result': task.get('result'),
    })


# ===== 下载 / 预览 =====

@pdfmerge_bp.route('/api/download/<task_id>')
@login_required
def api_download(task_id):
    """下载生成的 PDF"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    output_path = task.get('output_path') or (task.get('result') or {}).get('output_path')
    if not output_path or not os.path.isfile(output_path):
        return jsonify({'error': 'PDF文件不存在'}), 404

    return send_file(output_path, as_attachment=True,
                     download_name=os.path.basename(output_path))


@pdfmerge_bp.route('/api/preview/<task_id>')
@login_required
def api_preview(task_id):
    """在线预览 PDF"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    output_path = task.get('output_path') or (task.get('result') or {}).get('output_path')
    if not output_path or not os.path.isfile(output_path):
        return jsonify({'error': 'PDF文件不存在'}), 404

    return send_file(output_path, mimetype='application/pdf',
                     as_attachment=False)


@pdfmerge_bp.route('/api/save_to/<task_id>', methods=['POST'])
@login_required
def api_save_to(task_id):
    """另存为 - 弹出保存对话框"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    output_path = task.get('output_path') or (task.get('result') or {}).get('output_path')
    if not output_path or not os.path.isfile(output_path):
        return jsonify({'error': 'PDF文件不存在'}), 404

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        save_path = filedialog.asksaveasfilename(
            title='保存PDF文件',
            defaultextension='.pdf',
            filetypes=[('PDF文件', '*.pdf')],
            initialfile=os.path.basename(output_path),
        )
        root.destroy()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not save_path:
        return jsonify({'cancelled': True})

    import shutil
    shutil.copy2(output_path, save_path)
    logger.info(f'PDF另存为: {save_path}')

    return jsonify({'ok': True, 'saved_path': save_path})


# ===== 图片缩略图 =====

@pdfmerge_bp.route('/api/thumbnail/<image_id>')
@login_required
def api_thumbnail(image_id):
    """获取图片缩略图"""
    with _image_pool_lock:
        img_data = _image_pool.get(image_id)
    if not img_data:
        return jsonify({'error': '图片不存在'}), 404

    img_path = img_data['path']
    if not os.path.isfile(img_path):
        return jsonify({'error': '图片文件不存在'}), 404

    try:
        from PIL import Image
        import io

        img = Image.open(img_path)
        max_width = 200
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        img.close()
        buf.seek(0)

        return Response(buf.getvalue(), mimetype='image/jpeg')
    except Exception as e:
        logger.error(f'缩略图生成失败: {e}')
        return jsonify({'error': str(e)}), 500


# ===== 系统能力检测 =====

@pdfmerge_bp.route('/api/capabilities')
@login_required
def api_capabilities():
    """检查系统文件转换能力"""
    from .core.image_converter import check_conversion_capability
    return jsonify(check_conversion_capability())


# ===== 清空图片池 =====

@pdfmerge_bp.route('/api/clear', methods=['POST'])
@login_required
def api_clear():
    """清空图片池和临时文件"""
    with _image_pool_lock:
        count = len(_image_pool)
        _image_pool.clear()

    _processed_files.clear()

    # 清理临时图片文件
    try:
        import ctypes
        for f in os.listdir(IMAGE_TEMP_DIR):
            fp = os.path.join(IMAGE_TEMP_DIR, f)
            try:
                os.remove(fp)
            except Exception:
                pass
    except Exception:
        pass

    logger.info(f'图片池已清空 ({count} 张图片)')
    return jsonify({'ok': True, 'cleared': count})


# ===== 辅助函数 =====

def _update_task(task_id, **kwargs):
    """更新任务状态"""
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id].update(kwargs)
