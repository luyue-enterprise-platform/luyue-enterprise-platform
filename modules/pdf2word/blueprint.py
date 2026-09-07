# -*- coding: utf-8 -*-
"""
批量 PDF 转 WORD 模块 — Flask Blueprint（v2.0.0 可逆转换）

v2.0.0 新增：
- 转换方向 direction: 'pdf2word'（原有，逻辑不变）| 'topdf'（新增，独立隔离）
- 输出模式 output_mode（仅 topdf 生效）: 'merge'（合并单 PDF）| 'individual'（独立 PDF）
- 文件夹递归上传：/api/pick_folder + pick_ids 上传复用
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
from .core import to_pdf
from . import __version__ as MODULE_VERSION
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

# ===== 文件夹选择暂存（v2.0.0，转PDF方向文件夹递归上传） =====
# pick_id -> {'folder': 原始文件夹路径, 'files': [递归解析出的文件绝对路径, 按顺序]}
picked_folders = {}
picked_folders_lock = threading.Lock()


# ===== 路由 =====

@pdf2word_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('pdf2word_index.html')


# ---------- 上传并转换 ----------

@pdf2word_bp.route('/api/upload', methods=['POST'])
def api_upload():
    """上传文件并启动后台转换任务

    v2.0.0：按 direction 分流
    - direction=pdf2word（默认/原有）：仅收 .pdf，走 batch_convert（逻辑不变）
    - direction=topdf（新增）：收全部支持格式，可走 pick_ids 复用文件夹选择，
      output_mode=merge|individual
    """
    direction = request.form.get('direction', 'pdf2word')
    if direction not in ('pdf2word', 'topdf'):
        return jsonify({'error': '未知的转换方向: %s' % direction}), 400
    output_mode = request.form.get('output_mode', 'merge')
    if output_mode not in ('merge', 'individual'):
        output_mode = 'merge'

    files = request.files.getlist('files')
    # v2.1.1 修复：多文件夹选择必须全部消费——get() 只返回同名字段第一个值，
    # 导致多个 pick_ids 只有第一个文件夹被转换、其余静默丢弃。
    # getlist 收取全部重复字段，并兼容逗号拼接形式。
    pick_ids = []
    for v in request.form.getlist('pick_ids'):
        pick_ids.extend(p for p in str(v).split(',') if p)

    has_files = files and not (len(files) == 1 and files[0].filename == '')
    if not has_files and not pick_ids:
        return jsonify({'error': '请选择 PDF 文件' if direction == 'pdf2word' else '请选择文件或文件夹'}), 400

    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保存上传文件（保持上传先后顺序；重名自动加序号防覆盖）
    saved_paths = []
    skipped = []
    used_names = set()

    def _save_into(task_dir_, src_name, save_fn):
        base, ext = os.path.splitext(os.path.basename(src_name))
        name = base + ext
        n = 1
        while name in used_names:
            name = '%s(%d)%s' % (base, n, ext)
            n += 1
        used_names.add(name)
        fp = os.path.join(task_dir_, name)
        save_fn(fp)
        return fp

    if has_files:
        for f in files:
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if direction == 'pdf2word':
                if ext != '.pdf':
                    skipped.append({'name': f.filename, 'reason': '仅支持 PDF 文件'})
                    continue
            else:
                if not to_pdf.is_supported(f.filename):
                    skipped.append({'name': f.filename,
                                    'reason': '不支持的格式 "%s"（支持：图片/Word/Excel/TXT/PDF）' % ext})
                    continue
            saved_paths.append(_save_into(task_dir, f.filename, f.save))

    # 文件夹选择（pick_ids）：按递归解析顺序追加
    with picked_folders_lock:
        picks = [picked_folders.pop(pid, None) for pid in pick_ids]
    for pick in picks:
        if not pick:
            # v2.1.1：失效的 pick（已被消费/服务重启/会话过期）明确提示，不再静默跳过
            skipped.append({'name': '一个文件夹选择',
                            'reason': '该文件夹选择已失效（可能已转换过或程序已重启），请移除后重新选择文件夹'})
            continue
        for src_path in pick['files']:
            try:
                saved_paths.append(
                    _save_into(task_dir, src_path,
                               lambda fp, sp=src_path: shutil.copy2(sp, fp)))
            except Exception as e:
                skipped.append({'name': os.path.basename(src_path),
                                'reason': '复制失败: %s' % e})

    if not saved_paths:
        hint = '没有找到有效的 PDF 文件' if direction == 'pdf2word' else '没有找到可转换的文件'
        return jsonify({'error': hint, 'skipped': skipped}), 400

    # 初始化任务状态
    with tasks_lock:
        tasks[task_id] = {
            'status': 'processing',
            'current': 0,
            'total': len(saved_paths),
            'message': '正在转换 PDF...' if direction == 'pdf2word' else '正在转换为 PDF...',
            'results': None,
            'skipped': skipped,
            'direction': direction,
            'output_mode': output_mode if direction == 'topdf' else None,
        }

    # 启动后台转换线程
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(output_task_dir, exist_ok=True)

    if direction == 'topdf':
        thread = threading.Thread(
            target=_process_task_topdf,
            args=(task_id, saved_paths, output_task_dir, output_mode),
            daemon=True
        )
    else:
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
        'direction': direction,
        'output_mode': tasks[task_id]['output_mode'],
    })


# ---------- 文件夹递归选择（v2.0.0 新增，仅转PDF方向使用） ----------

@pdf2word_bp.route('/api/pick_folder', methods=['POST'])
@login_required
def api_pick_folder():
    """弹出系统原生文件夹选择对话框，递归解析其中所有支持格式的文件"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(
            title='选择文件夹（将递归解析其中所有可转换文件）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error('选择文件夹对话框异常: %s' % e)
        return jsonify({'error': '无法打开文件夹选择对话框: %s' % e}), 500

    if not folder:
        return jsonify({'cancelled': True})

    # 递归解析：按目录遍历顺序收集所有支持格式的文件
    found = []
    unsupported = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            if to_pdf.is_supported(fn):
                found.append(fp)
            else:
                unsupported += 1

    if not found:
        return jsonify({'error': '该文件夹中没有可转换的文件（支持：图片/Word/Excel/TXT/PDF）'}), 400

    pick_id = uuid.uuid4().hex[:8]
    with picked_folders_lock:
        picked_folders[pick_id] = {'folder': folder, 'files': found}

    return jsonify({
        'ok': True,
        'pick_id': pick_id,
        'folder': folder,
        'file_count': len(found),
        'unsupported_count': unsupported,
        'files': [os.path.basename(p) for p in found],
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


def _process_task_topdf(task_id, file_paths, output_dir, output_mode):
    """后台"转PDF"任务（v2.0.0 新增，与 _process_task 完全隔离）

    output_mode: 'merge' 合并为单个 PDF | 'individual' 每文件独立 PDF
    单文件失败不中断；结束时产出成功/失败清单与数量统计。
    """
    try:
        def progress_callback(current, total):
            with tasks_lock:
                tasks[task_id]['current'] = current
                tasks[task_id]['message'] = '正在转换为 PDF (%d/%d)' % (current, total)

        merged_name = '合并结果.pdf'
        results, failed = to_pdf.batch_to_pdf(
            file_paths, output_dir,
            output_mode=output_mode,
            progress_callback=progress_callback,
            merged_name=merged_name,
        )

        # 上传阶段剔除的 + 转换阶段失败的，合并为统一失败清单
        with tasks_lock:
            upload_skipped = tasks[task_id].get('skipped') or []
        all_failed = upload_skipped + failed
        success_count = len(results)
        fail_count = len(all_failed)

        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = (
                '转换完成！成功 %d 个，失败 %d 个' % (success_count, fail_count)
            )
            tasks[task_id]['results'] = results
            tasks[task_id]['skipped'] = all_failed

        logger.info('[task:%s] topdf %s', task_id, tasks[task_id]['message'])

    except Exception as e:
        logger.error('[task:%s] topdf 任务失败: %s\n%s', task_id, e, traceback.format_exc())
        with tasks_lock:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = '转换失败: %s' % e


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
        'direction': task.get('direction', 'pdf2word'),
        'output_mode': task.get('output_mode'),
    })


# ---------- 下载全部（ZIP） ----------

@pdf2word_bp.route('/api/download/<task_id>')
def api_download(task_id):
    """下载所有转换结果（打包为 ZIP）"""
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)

    if not os.path.exists(output_task_dir):
        return jsonify({'error': '输出目录不存在'}), 404

    with tasks_lock:
        task = tasks.get(task_id) or {}
    zip_name = ('转PDF结果.zip' if task.get('direction') == 'topdf'
                else 'PDF转Word结果.zip')

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
        download_name=zip_name
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
    return jsonify({'ok': True, 'version': MODULE_VERSION,
                    'time': datetime.now().isoformat()})


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
