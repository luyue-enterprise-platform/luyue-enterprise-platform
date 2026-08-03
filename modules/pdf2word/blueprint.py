# -*- coding: utf-8 -*-
"""
批量 PDF 转 WORD 模块 — Flask Blueprint
"""

import os
import sys
import uuid
import zipfile
import shutil
import logging
import traceback
import threading
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify,
    send_file
)

from .core.converter import batch_convert
from core.auth import login_required

# ===== 路径设置 =====
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'pdf2word')
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===== Blueprint 创建 =====
pdf2word_bp = Blueprint(
    'pdf2word',
    __name__,
    url_prefix='/pdf2word',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static')
)

UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
OUTPUT_DIR = os.path.join(DATA_DIR, 'outputs')
LOG_DIR = os.path.join(DATA_DIR, 'logs')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger('pdf2word')

# ===== 任务状态存储 =====
tasks = {}
tasks_lock = threading.Lock()


# ===== 路由 =====

@pdf2word_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('pdf2word_index.html')


# ---------- 上传并转换 ----------

@pdf2word_bp.route('/api/upload', methods=['POST'])
def api_upload():
    """上传 PDF 文件并启动后台转换任务"""
    files = request.files.getlist('files')
    if not files or (len(files) == 1 and files[0].filename == ''):
        return jsonify({'error': '请选择 PDF 文件'}), 400

    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保存所有 PDF 文件
    saved_paths = []
    skipped = []
    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext != '.pdf':
            skipped.append(f.filename)
            continue
        safe_name = os.path.basename(f.filename)
        fp = os.path.join(task_dir, safe_name)
        f.save(fp)
        saved_paths.append(fp)

    if not saved_paths:
        return jsonify({'error': '没有找到有效的 PDF 文件' + (f'，跳过了: {", ".join(skipped)}' if skipped else '')}), 400

    # 初始化任务状态
    with tasks_lock:
        tasks[task_id] = {
            'status': 'processing',
            'current': 0,
            'total': len(saved_paths),
            'message': '正在转换 PDF...',
            'results': None,
            'skipped': skipped,
        }

    # 启动后台转换线程
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(output_task_dir, exist_ok=True)

    thread = threading.Thread(
        target=_process_task,
        args=(task_id, saved_paths, output_task_dir),
        daemon=True
    )
    thread.start()

    return jsonify({
        'ok': True,
        'task_id': task_id,
        'total_files': len(saved_paths),
        'skipped': skipped,
    })


def _process_task(task_id, pdf_paths, output_dir):
    """后台转换任务"""
    try:
        def progress_callback(current, total, filename, result):
            with tasks_lock:
                tasks[task_id]['current'] = current
                status = '成功' if result['ok'] else '失败'
                tasks[task_id]['message'] = f'正在转换 ({current}/{total}): {filename} - {status}'

        results = batch_convert(pdf_paths, output_dir, progress_callback)

        success_count = sum(1 for r in results if r['ok'])
        fail_count = len(results) - success_count

        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = (
                f'转换完成！成功 {success_count} 个，失败 {fail_count} 个'
            )
            tasks[task_id]['results'] = results

        logger.info(f'[task:{task_id}] {tasks[task_id]["message"]}')

    except Exception as e:
        logger.error(f'[task:{task_id}] 任务失败: {e}\n{traceback.format_exc()}')
        with tasks_lock:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = f'转换失败: {str(e)}'


# ---------- 进度查询 ----------

@pdf2word_bp.route('/api/progress/<task_id>')
def api_progress(task_id):
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify({
        'status': task['status'],
        'current': task['current'],
        'total': task['total'],
        'message': task['message'],
    })


# ---------- 结果查询 ----------

@pdf2word_bp.route('/api/result/<task_id>')
def api_result(task_id):
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404

    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成', 'status': task['status']}), 400

    return jsonify({
        'ok': True,
        'results': task['results'],
        'skipped': task.get('skipped', []),
    })


# ---------- 下载全部（ZIP） ----------

@pdf2word_bp.route('/api/download/<task_id>')
def api_download(task_id):
    """下载所有转换后的 Word 文件（打包为 ZIP）"""
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)

    if not os.path.exists(output_task_dir):
        return jsonify({'error': '输出目录不存在'}), 404

    zip_path = os.path.join(OUTPUT_DIR, f'{task_id}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(output_task_dir):
            for f in files:
                file_path = os.path.join(root, f)
                arcname = os.path.relpath(file_path, output_task_dir)
                zf.write(file_path, arcname)

    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name='PDF转Word结果.zip'
    )


# ---------- 下载单个文件 ----------

@pdf2word_bp.route('/api/download_file/<task_id>/<filename>')
def api_download_file(task_id, filename):
    """下载单个转换后的 Word 文件"""
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    file_path = os.path.join(output_task_dir, filename)

    if not os.path.isfile(file_path):
        return jsonify({'error': '文件不存在'}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename
    )


# ---------- 健康检查 ----------

@pdf2word_bp.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'time': datetime.now().isoformat()})


# ---------- 保存到指定位置（弹出系统原生文件夹选择对话框） ----------

@pdf2word_bp.route('/api/save_to/<task_id>', methods=['POST'])
@login_required
def api_save_to(task_id):
    """弹出系统原生文件夹选择对话框，将转换后的Word文件保存到用户选择的位置"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    if not os.path.exists(output_task_dir):
        return jsonify({'error': '输出目录不存在'}), 404

    # 可选：只保存单个文件
    single_file = request.form.get('file_name') or (request.json or {}).get('file_name') if request.is_json else request.form.get('file_name')

    # 使用 tkinter 弹出系统原生文件夹选择对话框
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        save_dir = filedialog.askdirectory(
            title='选择保存位置（Word文件将保存到此文件夹）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'保存对话框异常: {e}')
        return jsonify({'error': f'无法打开保存对话框: {e}'}), 500

    if not save_dir:
        return jsonify({'cancelled': True})

    # 复制文件到用户选择的目录
    file_count = 0
    if single_file:
        # 只保存指定文件
        src_path = os.path.join(output_task_dir, single_file)
        if os.path.isfile(src_path):
            dest_path = os.path.join(save_dir, single_file)
            shutil.copy2(src_path, dest_path)
            file_count = 1
    else:
        # 保存全部文件
        for item in os.listdir(output_task_dir):
            src_path = os.path.join(output_task_dir, item)
            dest_path = os.path.join(save_dir, item)
            if os.path.isfile(src_path):
                shutil.copy2(src_path, dest_path)
                file_count += 1

    logger.info(f'[task:{task_id}] Word文件已保存到: {save_dir} ({file_count}个文件)')

    return jsonify({
        'ok': True,
        'save_dir': save_dir,
        'file_count': file_count,
    })
