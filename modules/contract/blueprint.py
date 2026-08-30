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
from .core.file_renamer import (
    plan_renames, execute_renames, rollback_renames,
    validate_renames, IMAGE_EXTENSIONS,
)
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

# ===== 文件夹选择临时存储 =====
picked_folders = {}  # {pick_id: {'file_paths': [(path, folder_name)], 'files': [...]}}

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
    """上传劳动合同图片/PDF并启动后台处理任务
    支持混合模式：同时上传文件 + 多个文件夹选择(pick_ids)
    """
    roster_json = request.form.get('roster', '')
    if not roster_json:
        return jsonify({'error': '请先上传花名册'}), 400

    try:
        roster = json.loads(roster_json)
    except json.JSONDecodeError:
        return jsonify({'error': '花名册数据格式错误'}), 400

    if not roster:
        return jsonify({'error': '花名册为空'}), 400

    # 获取上传的文件
    files = request.files.getlist('files')
    has_uploaded_files = files and not (len(files) == 1 and files[0].filename == '')

    # 获取文件夹选择的 pick_ids（逗号分隔，支持多个文件夹）
    pick_ids_str = request.form.get('pick_ids', '')
    pick_ids = [pid.strip() for pid in pick_ids_str.split(',') if pid.strip()] if pick_ids_str else []

    if not has_uploaded_files and not pick_ids:
        return jsonify({'error': '请选择劳动合同文件或文件夹'}), 400

    # 读取文件夹映射（文件索引 -> 文件夹名）—— 仅对上传的文件有效
    folder_map = {}
    folder_map_json = request.form.get('folder_map', '')
    if folder_map_json:
        try:
            folder_map = json.loads(folder_map_json)
        except json.JSONDecodeError:
            pass

    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保存所有文件，同时记录文件夹名
    saved_paths = []
    folder_hints = {}  # {saved_path: folder_name}

    # 1. 保存上传的文件
    if has_uploaded_files:
        for idx, f in enumerate(files):
            if f.filename:
                safe_name = os.path.basename(f.filename)
                fp = os.path.join(task_dir, safe_name)
                f.save(fp)
                saved_paths.append(fp)
                # 如果该文件来自文件夹，记录文件夹名
                folder_name = folder_map.get(str(idx), '')
                if folder_name:
                    folder_hints[fp] = folder_name

    # 2. 从 picked_folders 复制文件（支持多个 pick_id）
    for pick_id in pick_ids:
        with tasks_lock:
            picked = picked_folders.get(pick_id)

        if not picked:
            logger.warning(f'[upload] pick_id {pick_id} 已过期，跳过')
            continue

        for idx2, (src_path, folder_name) in enumerate(picked['file_paths']):
            safe_name = os.path.basename(src_path)
            dest_path = os.path.join(task_dir, safe_name)
            # 避免重名
            if os.path.exists(dest_path):
                dest_path = os.path.join(task_dir, f'p{pick_id[:4]}_{idx2}_{safe_name}')
            try:
                shutil.copy2(src_path, dest_path)
            except Exception as e:
                logger.error(f'复制文件失败: {src_path} -> {dest_path}: {e}')
                continue
            saved_paths.append(dest_path)
            if folder_name:
                folder_hints[dest_path] = folder_name

        # 清理 picked_folders 中的临时数据
        with tasks_lock:
            picked_folders.pop(pick_id, None)

    if not saved_paths:
        return jsonify({'error': '没有有效的文件可处理'}), 400

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
        args=(task_id, saved_paths, roster, folder_hints),
        daemon=True
    )
    thread.start()

    return jsonify({'ok': True, 'task_id': task_id, 'total_files': len(saved_paths)})


# ---------- 文件夹选择（原生对话框） ----------

@contract_bp.route('/api/pick_folder', methods=['POST'])
@login_required
def api_pick_folder():
    """弹出系统原生文件夹选择对话框，读取文件夹中的图片/PDF文件"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        folder_path = filedialog.askdirectory(
            title='选择劳动合同文件夹（以姓名命名的文件夹或包含多个人员文件夹的目录）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件夹选择对话框异常: {e}')
        return jsonify({'error': f'无法打开文件夹选择对话框: {e}'}), 500

    if not folder_path:
        return jsonify({'cancelled': True})

    # 递归遍历文件夹，查找所有图片和PDF文件
    # v1.1.41: 最多穿透五级子文件夹——所选文件夹根目录为第0级，其下1~5级子文件夹内的
    # 图片/PDF文件均可读取；到达第5级后不再向下穿透。文件夹本身一律跳过（不作为文件读取），
    # 仅收集图片/PDF文件的文件名
    MAX_TRAVERSE_DEPTH = 5
    valid_exts = IMAGE_EXTENSIONS | {'.pdf'}
    file_list = []        # [{name, folder, size}]
    file_paths = []       # [(full_path, folder_name)]

    picked_folder_name = os.path.basename(folder_path)

    for dirpath, dirnames, filenames in os.walk(folder_path):
        # 计算相对路径
        rel_dir = os.path.relpath(dirpath, folder_path)

        # 深度控制：当前目录已是第五级子文件夹时，不再向下穿透
        depth = 0 if rel_dir == '.' else rel_dir.count(os.sep) + 1
        if depth >= MAX_TRAVERSE_DEPTH:
            dirnames[:] = []

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in valid_exts:
                continue

            full_path = os.path.join(dirpath, fname)

            # 确定用于人员匹配的文件夹名
            if rel_dir == '.':
                # 文件在所选文件夹根目录 -> 用所选文件夹名
                folder_name = picked_folder_name
            else:
                # 文件在子文件夹 -> 用直接父文件夹名
                folder_name = os.path.basename(dirpath)

            file_paths.append((full_path, folder_name))
            file_list.append({
                'name': fname,
                'folder': folder_name,
                'size': os.path.getsize(full_path),
            })

    if not file_list:
        return jsonify({'error': '所选文件夹中没有找到图片或PDF文件（支持 .jpg .png .bmp .pdf 等）'})

    # 存储选中文件信息，等待后续处理
    pick_id = uuid.uuid4().hex[:8]
    with tasks_lock:
        picked_folders[pick_id] = {
            'file_paths': file_paths,
            'files': file_list,
        }

    logger.info(f'[pick:{pick_id}] 用户选择文件夹: {folder_path}, 找到 {len(file_list)} 个文件')

    return jsonify({
        'ok': True,
        'pick_id': pick_id,
        'folder_name': picked_folder_name,
        'files': file_list,
        'count': len(file_list),
    })


@contract_bp.route('/api/process_picked', methods=['POST'])
@login_required
def api_process_picked():
    """处理通过 pick_folder 选择的本地文件"""
    pick_id = request.form.get('pick_id', '')
    roster_json = request.form.get('roster', '')

    if not pick_id:
        return jsonify({'error': '请先选择文件夹'}), 400

    with tasks_lock:
        picked = picked_folders.get(pick_id)

    if not picked:
        return jsonify({'error': '文件夹选择已过期，请重新选择'}), 400

    try:
        roster = json.loads(roster_json)
    except json.JSONDecodeError:
        return jsonify({'error': '花名册数据格式错误'}), 400

    if not roster:
        return jsonify({'error': '花名册为空'}), 400

    file_paths_with_folders = picked['file_paths']

    # 复制文件到任务上传目录，同时构建 folder_hints
    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    saved_paths = []
    folder_hints = {}
    for idx, (src_path, folder_name) in enumerate(file_paths_with_folders):
        safe_name = os.path.basename(src_path)
        dest_path = os.path.join(task_dir, safe_name)
        # 避免重名
        if os.path.exists(dest_path):
            dest_path = os.path.join(task_dir, f'{idx}_{safe_name}')
        shutil.copy2(src_path, dest_path)
        saved_paths.append(dest_path)
        if folder_name:
            folder_hints[dest_path] = folder_name

    # 清理 picked_folders 中的临时数据
    with tasks_lock:
        picked_folders.pop(pick_id, None)

    # 初始化任务状态
    with tasks_lock:
        tasks[task_id] = {
            'status': 'processing',
            'current': 0,
            'total': len(saved_paths),
            'message': '正在处理文件...',
            'result': None,
        }

    # 启动后台处理线程（复用已有的 _process_task）
    thread = threading.Thread(
        target=_process_task,
        args=(task_id, saved_paths, roster, folder_hints),
        daemon=True
    )
    thread.start()

    logger.info(f'[task:{task_id}] 从文件夹选择启动处理, 共 {len(saved_paths)} 个文件')

    return jsonify({'ok': True, 'task_id': task_id, 'total_files': len(saved_paths)})


def _process_task(task_id, file_paths, roster, folder_hints=None):
    """后台处理任务"""
    try:
        # Step 1: PDF转图片
        with tasks_lock:
            tasks[task_id]['message'] = '正在将PDF转换为图片...'

        all_images = []
        pdf_converted = 0
        # 扩展 folder_hints 到 PDF 转换后的图片
        extended_hints = dict(folder_hints or {})

        for fp in file_paths:
            ext = os.path.splitext(fp)[1].lower()
            if ext == '.pdf':
                try:
                    page_images = pdf_to_images(fp, output_dir=os.path.dirname(fp))
                    all_images.extend(page_images)
                    pdf_converted += 1
                    # PDF的文件夹名继承到转换后的图片
                    folder_name = (folder_hints or {}).get(fp, '')
                    if folder_name:
                        for img_path in page_images:
                            extended_hints[img_path] = folder_name
                    logger.info(f'[task:{task_id}] PDF {os.path.basename(fp)} -> {len(page_images)} 页')
                except Exception as e:
                    logger.error(f'[task:{task_id}] PDF转换失败 {fp}: {e}')
            elif ext in IMAGE_EXTENSIONS:
                all_images.append(fp)
            # 忽略不支持的文件类型

        # 更新总数
        with tasks_lock:
            tasks[task_id]['total'] = len(all_images)
            tasks[task_id]['message'] = f'共 {len(all_images)} 张图片，正在按花名册分析...'

        if not all_images:
            with tasks_lock:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = '没有找到可处理的图片文件'
            return

        # Step 2: 按花名册智能匹配，生成重命名计划（不执行，进入 preview 等待用户确认）
        plan = plan_renames(all_images, roster, folder_hints=extended_hints)
        source_paths = {os.path.basename(fp): fp for fp in all_images}

        with tasks_lock:
            tasks[task_id]['status'] = 'preview'
            tasks[task_id]['message'] = (
                f'分析完成：自动匹配 {len(plan["auto"])} 个，'
                f'重名待确认 {len(plan["duplicates"])} 个，'
                f'未匹配 {len(plan["unmatched"])} 个，请预览确认后执行重命名'
            )
            tasks[task_id]['plan'] = plan
            tasks[task_id]['plan_paths'] = source_paths
            tasks[task_id]['roster'] = roster
            tasks[task_id]['pdf_converted'] = pdf_converted

        logger.info(
            f'[task:{task_id}] 计划生成: 自动 {len(plan["auto"])}, '
            f'重名 {len(plan["duplicates"])}, 未匹配 {len(plan["unmatched"])}'
        )

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

    resp = {
        'status': task['status'],
        'current': task['current'],
        'total': task['total'],
        'message': task['message'],
    }

    return jsonify(resp)


# ---------- 预览重命名计划 ----------

@contract_bp.route('/api/preview/<task_id>')
@login_required
def api_preview(task_id):
    """获取重命名计划（preview 状态），供前端渲染预览界面（不含服务器路径）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'preview':
        return jsonify({'error': '任务当前不在预览阶段', 'status': task['status']}), 400

    plan = task.get('plan') or {}
    return jsonify({
        'ok': True,
        'plan': {
            'auto': [
                {
                    'original': item['original'],
                    'seq': item['seq'],
                    'name': item['name'],
                    'new_name': item['new_name'],
                }
                for item in plan.get('auto', [])
            ],
            'duplicates': [
                {
                    'original': item['original'],
                    'guessed': item.get('guessed', ''),
                    'reason': item.get('reason', ''),
                    'candidates': item.get('candidates', []),
                }
                for item in plan.get('duplicates', [])
            ],
            'unmatched': plan.get('unmatched', []),
            'roster_missing': plan.get('roster_missing', []),
            'total': plan.get('total', 0),
        },
    })


# ---------- 执行重命名 ----------

@contract_bp.route('/api/execute/<task_id>', methods=['POST'])
@login_required
def api_execute(task_id):
    """执行重命名（用户在预览界面确认/调整后提交）

    请求体 JSON:
    {
        "renames": [
            {"original": "01张三.jpg", "new_name": "01-张三-002X.jpg", "seq": 1},
            ...
        ],
        "pending": ["无名人.jpg", ...]   // 用户标记移入待处理的文件
    }
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'preview':
        return jsonify({'error': '任务当前不在预览阶段', 'status': task['status']}), 400

    data = request.get_json(silent=True) or {}
    renames = data.get('renames', [])
    pending = data.get('pending', [])

    if not renames and not pending:
        return jsonify({'error': '没有可执行的重命名项'}), 400

    source_paths = task.get('plan_paths') or {}
    plan = task.get('plan') or {}
    roster = task.get('roster') or []
    pdf_converted = task.get('pdf_converted') or 0

    # 仅接受计划内文件
    valid_originals = set(source_paths.keys())
    renames = [r for r in renames if r.get('original') in valid_originals]
    pending = [p for p in pending if p in valid_originals]
    if not renames and not pending:
        return jsonify({'error': '提交的文件均不在重命名计划内'}), 400

    # 同步校验（错误立即返回，不进线程）
    errors = validate_renames(source_paths, renames)
    if errors:
        return jsonify({'error': '；'.join(errors[:5])}), 400

    with tasks_lock:
        task['status'] = 'processing'
        task['message'] = f'正在执行重命名 {len(renames)}/{len(renames) + len(pending)} ...'

    thread = threading.Thread(
        target=_execute_task,
        args=(task_id, source_paths, plan, roster, renames, pending, pdf_converted),
        daemon=True,
    )
    thread.start()

    logger.info(f'[task:{task_id}] 用户确认重命名计划，执行 {len(renames)} 个，待处理 {len(pending)} 个')

    return jsonify({'ok': True, 'task_id': task_id, 'renaming': len(renames)})


def _execute_task(task_id, source_paths, plan, roster, renames, pending, pdf_converted):
    """执行重命名的后台线程"""
    try:
        output_task_dir = os.path.join(OUTPUT_DIR, task_id)
        os.makedirs(output_task_dir, exist_ok=True)

        def _progress(cur, tot):
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['current'] = cur
                    tasks[task_id]['message'] = f'正在重命名 {cur}/{tot} ...'

        result = execute_renames(
            source_paths, plan, output_task_dir, renames, pending,
            progress_callback=_progress, task_id=task_id,
        )

        task_result = {
            'renamed': result['renamed'],
            'unmatched': result['unmatched'],
            'total': result['total'],
            'matched_count': result['matched_count'],
            'unmatched_count': result['unmatched_count'],
            'conflicts_resolved_count': result['conflicts_resolved_count'],
            'roster_count': len(roster),
            'pdf_converted': pdf_converted,
            'pending_dir_name': '待处理',
        }

        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return
            task['status'] = 'done'
            task['message'] = (
                f'处理完成！共 {result["total"]} 个文件，'
                f'重命名 {result["matched_count"]} 个，'
                f'待处理 {result["unmatched_count"]} 个'
            )
            task['result'] = task_result
            # 清理预览阶段数据
            task['plan'] = None
            task['plan_paths'] = None
            task['roster'] = None

        logger.info(f'[task:{task_id}] 重命名执行完成: {task["message"]}')

    except Exception as e:
        logger.error(f'[task:{task_id}] 重命名执行失败: {e}\n{traceback.format_exc()}')
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = f'重命名执行失败: {str(e)}'


# ---------- 回滚重命名 ----------

@contract_bp.route('/api/rollback/<task_id>', methods=['POST'])
@login_required
def api_rollback(task_id):
    """依据重命名日志回滚输出目录中的文件（恢复原文件名）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成，无法回滚', 'status': task['status']}), 400

    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    rollback = rollback_renames(output_task_dir)

    if 'error' in rollback:
        return jsonify({'error': rollback['error']}), 400

    # 更新任务结果：清空重命名明细，记录回滚数量
    with tasks_lock:
        task = tasks.get(task_id)
        if task and task.get('result'):
            tr = task['result']
            tr['rolled_back_count'] = rollback.get('reverted', 0)
            tr['renamed'] = []
            tr['matched_count'] = 0
            task['message'] = f'已回滚 {rollback.get("reverted", 0)} 个文件至原文件名'

    logger.info(
        f'[task:{task_id}] 回滚完成: 恢复 {rollback.get("reverted", 0)} 个, '
        f'失败 {rollback.get("failed", 0)} 个'
    )

    return jsonify({
        'ok': True,
        'task_id': task_id,
        'reverted': rollback.get('reverted', 0),
        'failed': rollback.get('failed', 0),
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
