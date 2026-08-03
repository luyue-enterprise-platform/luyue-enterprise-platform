"""劳动合同图片整理系统 — Flask Blueprint"""

import os
import sys
import json
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

from .core.roster_parser import parse_roster_from_table
from .core.pdf_converter import pdf_to_images
from .core.file_renamer import rename_contract_images, IMAGE_EXTENSIONS
from core.auth import login_required

# ===== 路径设置 =====
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    # PyInstaller 单文件模式：模板/静态资源按 build.spec 中的 (src, dst) 存放
    # build.spec 将 modules/contract/templates 打包到 modules/contract/templates
    # 因此本蓝图的 RESOURCE_DIR 应为 sys._MEIPASS/modules/contract
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'contract')
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===== Blueprint 创建 =====
contract_bp = Blueprint(
    'contract',
    __name__,
    url_prefix='/contract',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static')
)

UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
OUTPUT_DIR = os.path.join(DATA_DIR, 'outputs')
LOG_DIR = os.path.join(DATA_DIR, 'logs')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ===== 日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'app.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('contract_organizer')

# ===== 任务状态存储 =====
tasks = {}
tasks_lock = threading.Lock()

# ===== 路由 =====

@contract_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('contract_index.html')


# ---------- 花名册上传 ----------

@contract_bp.route('/api/roster', methods=['POST'])
def api_roster():
    """上传并解析花名册"""
    file = request.files.get('file')
    if not file:
        return jsonify({'error': '请选择花名册文件'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ('xlsx', 'xls', 'csv'):
        return jsonify({'error': f'不支持的花名册格式: .{ext}，请使用 .xlsx / .xls / .csv'}), 400

    # 保存临时文件
    tmp_path = os.path.join(UPLOAD_DIR, f'roster_{uuid.uuid4().hex[:8]}.{ext}')
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    file.save(tmp_path)

    try:
        persons = parse_roster_from_table(tmp_path)
        if not persons:
            return jsonify({'error': '花名册解析结果为空，请检查文件内容'}), 400

        # 清理临时文件
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return jsonify({
            'ok': True,
            'persons': persons,
            'count': len(persons),
        })
    except Exception as e:
        logger.error(f'花名册解析失败: {e}\n{traceback.format_exc()}')
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return jsonify({'error': f'花名册解析失败: {str(e)}'}), 400


# ---------- 文件上传并处理 ----------

@contract_bp.route('/api/upload', methods=['POST'])
def api_upload():
    """上传劳动合同图片/PDF并启动后台处理任务"""
    roster_json = request.form.get('roster', '')
    if not roster_json:
        return jsonify({'error': '请先上传花名册'}), 400

    try:
        roster = json.loads(roster_json)
    except json.JSONDecodeError:
        return jsonify({'error': '花名册数据格式错误'}), 400

    if not roster:
        return jsonify({'error': '花名册为空'}), 400

    files = request.files.getlist('files')
    if not files or (len(files) == 1 and files[0].filename == ''):
        return jsonify({'error': '请选择劳动合同文件'}), 400

    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保存所有文件
    saved_paths = []
    for f in files:
        if f.filename:
            safe_name = os.path.basename(f.filename)
            fp = os.path.join(task_dir, safe_name)
            f.save(fp)
            saved_paths.append(fp)

    # 初始化任务状态
    with tasks_lock:
        tasks[task_id] = {
            'status': 'processing',
            'current': 0,
            'total': len(saved_paths),
            'message': '正在处理文件...',
            'result': None,
        }

    # 启动后台处理线程
    thread = threading.Thread(
        target=_process_task,
        args=(task_id, saved_paths, roster),
        daemon=True
    )
    thread.start()

    return jsonify({'ok': True, 'task_id': task_id, 'total_files': len(saved_paths)})


def _process_task(task_id, file_paths, roster):
    """后台处理任务"""
    try:
        # Step 1: PDF转图片
        with tasks_lock:
            tasks[task_id]['message'] = '正在将PDF转换为图片...'

        all_images = []
        pdf_converted = 0

        for fp in file_paths:
            ext = os.path.splitext(fp)[1].lower()
            if ext == '.pdf':
                try:
                    page_images = pdf_to_images(fp, output_dir=os.path.dirname(fp))
                    all_images.extend(page_images)
                    pdf_converted += 1
                    logger.info(f'[task:{task_id}] PDF {os.path.basename(fp)} -> {len(page_images)} 页')
                except Exception as e:
                    logger.error(f'[task:{task_id}] PDF转换失败 {fp}: {e}')
            elif ext in IMAGE_EXTENSIONS:
                all_images.append(fp)
            # 忽略不支持的文件类型

        # 更新总数
        with tasks_lock:
            tasks[task_id]['total'] = len(all_images)
            tasks[task_id]['message'] = f'共 {len(all_images)} 张图片，正在按花名册重命名...'

        if not all_images:
            with tasks_lock:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = '没有找到可处理的图片文件'
            return

        # Step 2: 按花名册重命名
        output_task_dir = os.path.join(OUTPUT_DIR, task_id)
        os.makedirs(output_task_dir, exist_ok=True)

        result = rename_contract_images(all_images, roster, output_task_dir)

        # 更新任务状态
        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = (
                f'处理完成！共 {result["total"]} 张图片，'
                f'匹配 {result["matched_count"]} 个，'
                f'未匹配 {result["unmatched_count"]} 个'
            )
            tasks[task_id]['result'] = {
                'renamed': result['renamed'],
                'unmatched': result['unmatched'],
                'total': result['total'],
                'matched_count': result['matched_count'],
                'unmatched_count': result['unmatched_count'],
                'roster_count': len(roster),
                'pdf_converted': pdf_converted,
            }

        logger.info(f'[task:{task_id}] 任务完成: {tasks[task_id]["message"]}')

    except Exception as e:
        logger.error(f'[task:{task_id}] 任务失败: {e}\n{traceback.format_exc()}')
        with tasks_lock:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = f'处理失败: {str(e)}'


# ---------- 进度查询 ----------

@contract_bp.route('/api/progress/<task_id>')
def api_progress(task_id):
    """查询任务进度"""
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

@contract_bp.route('/api/result/<task_id>')
def api_result(task_id):
    """获取任务结果"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404

    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成', 'status': task['status']}), 400

    return jsonify({
        'ok': True,
        'result': task['result'],
    })


# ---------- 下载重命名后的文件 ----------

@contract_bp.route('/api/download/<task_id>')
def api_download(task_id):
    """下载重命名后的文件（打包为ZIP）"""
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)

    if not os.path.exists(output_task_dir):
        return jsonify({'error': '输出目录不存在，请先处理文件'}), 404

    # 打包为 ZIP
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
        download_name='劳动合同图片(已整理).zip'
    )


# ---------- 健康检查 ----------

@contract_bp.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'time': datetime.now().isoformat()})


# ---------- 保存到指定位置（弹出系统原生文件夹选择对话框） ----------

@contract_bp.route('/api/save_to/<task_id>', methods=['POST'])
@login_required
def api_save_to(task_id):
    """弹出系统原生文件夹选择对话框，将整理后的文件保存到用户选择的位置"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    if not os.path.exists(output_task_dir):
        return jsonify({'error': '输出目录不存在'}), 404

    # 使用 tkinter 弹出系统原生文件夹选择对话框
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        save_dir = filedialog.askdirectory(
            title='选择保存位置（整理后的文件将保存到此文件夹）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'保存对话框异常: {e}')
        return jsonify({'error': f'无法打开保存对话框: {e}'}), 500

    if not save_dir:
        return jsonify({'cancelled': True})

    # 将整理后的文件复制到用户选择的目录
    dest_dir = os.path.join(save_dir, '劳动合同图片(已整理)')
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    shutil.copytree(output_task_dir, dest_dir)

    # 统计文件数
    file_count = 0
    for root_dir, dirs, files in os.walk(dest_dir):
        file_count += len(files)

    logger.info(f'[task:{task_id}] 文件已保存到: {dest_dir} ({file_count}个文件)')

    return jsonify({
        'ok': True,
        'save_dir': save_dir,
        'saved_files': ['劳动合同图片(已整理)/'],
        'file_count': file_count,
    })
