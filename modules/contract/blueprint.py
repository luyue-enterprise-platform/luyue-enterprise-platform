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
    rename_contract_images, IMAGE_EXTENSIONS,
    write_failure_report, write_process_log,
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
    valid_exts = IMAGE_EXTENSIONS | {'.pdf'}
    file_list = []        # [{name, folder, size}]
    file_paths = []       # [(full_path, folder_name)]

    picked_folder_name = os.path.basename(folder_path)

    for dirpath, dirnames, filenames in os.walk(folder_path):
        # 计算相对路径
        rel_dir = os.path.relpath(dirpath, folder_path)

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
            tasks[task_id]['message'] = f'共 {len(all_images)} 张图片，正在按花名册重命名...'

        if not all_images:
            with tasks_lock:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = '没有找到可处理的图片文件'
            return

        # Step 2: 按花名册智能匹配并重命名（含冲突检测与待处理分流）
        output_task_dir = os.path.join(OUTPUT_DIR, task_id)
        os.makedirs(output_task_dir, exist_ok=True)

        def _progress(cur, tot):
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['current'] = cur
                    tasks[task_id]['message'] = f'正在匹配与重命名 {cur}/{tot} ...'

        result = rename_contract_images(
            all_images, roster, output_task_dir,
            folder_hints=extended_hints,
            progress_callback=_progress,
        )

        task_result = {
            'renamed': result['renamed'],
            'unmatched': result['unmatched'],
            'total': result['total'],
            'matched_count': result['matched_count'],
            'unmatched_count': result['unmatched_count'],
            'conflicts_resolved_count': 0,
            'roster_count': len(roster),
            'pdf_converted': pdf_converted,
            'pending_dir_name': '待处理',
        }

        # 冲突项：暂停处理，等待人工确认
        if result['conflicts']:
            with tasks_lock:
                tasks[task_id]['status'] = 'conflict'
                tasks[task_id]['message'] = (
                    f'发现 {len(result["conflicts"])} 个冲突文件，'
                    f'已暂停处理，请人工确认后继续'
                )
                tasks[task_id]['conflicts'] = result['conflicts']
                tasks[task_id]['roster'] = roster
                tasks[task_id]['result'] = task_result

            # 先行输出当前阶段（成功+失败）的报告与日志
            write_failure_report(output_task_dir, task_result['unmatched'])
            write_process_log(output_task_dir, task_id, task_result)

            logger.info(
                f'[task:{task_id}] 冲突暂停: {len(result["conflicts"])} 个冲突文件 '
                f'({result["matched_count"]} 个已正常重命名)'
            )
            return

        # 无冲突：直接完成
        write_failure_report(output_task_dir, task_result['unmatched'])
        write_process_log(output_task_dir, task_id, task_result)

        # 更新任务状态
        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = (
                f'处理完成！共 {result["total"]} 张图片，'
                f'匹配 {result["matched_count"]} 个，'
                f'待处理 {result["unmatched_count"]} 个'
            )
            tasks[task_id]['result'] = task_result

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

    resp = {
        'status': task['status'],
        'current': task['current'],
        'total': task['total'],
        'message': task['message'],
    }

    # 冲突暂停状态：返回冲突明细供前端渲染人工确认面板（不含服务器路径）
    if task['status'] == 'conflict':
        resp['conflicts'] = [
            {
                'original': c.get('original'),
                'guessed': c.get('guessed', ''),
                'reason': c.get('reason', ''),
                'candidates': c.get('candidates', []),
            }
            for c in (task.get('conflicts') or [])
        ]

    return jsonify(resp)


# ---------- 冲突人工确认 ----------

@contract_bp.route('/api/resolve/<task_id>', methods=['POST'])
@login_required
def api_resolve_conflicts(task_id):
    """冲突人工确认后继续处理

    请求体 JSON:
    {
        "resolutions": [
            {"original": "xxx.jpg", "seq": 3},            // 指定花名册人员
            {"original": "yyy.jpg", "action": "pending"}  // 移入待处理文件夹
        ]
    }
    未提交确认结果的冲突文件自动移入待处理文件夹。
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'conflict':
        return jsonify({'error': '任务当前没有待确认的冲突'}), 400

    data = request.get_json(silent=True) or {}
    resolutions_list = data.get('resolutions', [])

    conflicts = task.get('conflicts') or []
    conflict_names = {c['original'] for c in conflicts}

    # 构建确认映射（仅接受冲突清单内的文件）
    resolutions = {}
    user_confirmed = set()
    for r in resolutions_list:
        original = r.get('original')
        if not original or original not in conflict_names:
            continue
        if r.get('action') == 'pending':
            resolutions[original] = {'action': 'pending'}
            user_confirmed.add(original)
        elif r.get('seq') is not None:
            resolutions[original] = {'seq': r.get('seq')}
            user_confirmed.add(original)

    # 未提交确认结果的冲突文件 -> 自动移入待处理
    for c in conflicts:
        if c['original'] not in resolutions:
            resolutions[c['original']] = {'action': 'pending'}

    roster = task.get('roster') or []
    output_task_dir = os.path.join(OUTPUT_DIR, task_id)
    conflict_paths = [c['path'] for c in conflicts]

    if not conflict_paths or not roster:
        return jsonify({'error': '任务数据不完整，无法继续处理'}), 400

    with tasks_lock:
        task['status'] = 'processing'
        task['message'] = f'正在按人工确认结果处理 {len(conflict_paths)} 个冲突文件...'

    thread = threading.Thread(
        target=_resolve_task,
        args=(task_id, conflict_paths, roster, resolutions, user_confirmed),
        daemon=True,
    )
    thread.start()

    logger.info(f'[task:{task_id}] 冲突人工确认已提交，继续处理 {len(conflict_paths)} 个文件')

    return jsonify({'ok': True, 'task_id': task_id, 'resolving': len(conflict_paths)})


def _resolve_task(task_id, conflict_paths, roster, resolutions, user_confirmed):
    """冲突确认后的后台处理：按确认结果追加重命名并汇总"""
    try:
        output_task_dir = os.path.join(OUTPUT_DIR, task_id)

        result = rename_contract_images(
            conflict_paths, roster, output_task_dir,
            resolutions=resolutions,
        )

        # 未人工确认而被自动移入待处理的文件，修正失败原因
        for item in result['unmatched']:
            if (item.get('original') in resolutions
                    and item['original'] not in user_confirmed):
                item['reason'] = '冲突未确认，自动移入待处理文件夹'

        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return
            tr = task.get('result') or {
                'renamed': [], 'unmatched': [], 'total': 0,
                'matched_count': 0, 'unmatched_count': 0,
                'conflicts_resolved_count': 0, 'roster_count': len(roster),
                'pdf_converted': 0, 'pending_dir_name': '待处理',
            }

            tr['renamed'].extend(result['renamed'])
            tr['unmatched'].extend(result['unmatched'])
            tr['matched_count'] += result['matched_count']
            tr['unmatched_count'] += result['unmatched_count']
            tr['conflicts_resolved_count'] += len(resolutions)

            task['status'] = 'done'
            task['conflicts'] = []
            task['roster'] = None
            task['result'] = tr
            task['message'] = (
                f'处理完成！共 {tr["total"]} 张图片，'
                f'匹配 {tr["matched_count"]} 个，'
                f'待处理 {tr["unmatched_count"]} 个'
                f'（含人工确认冲突 {len(user_confirmed)} 个）'
            )

        # 重写失败明细报告与处理日志（覆盖初次的中间版本）
        write_failure_report(output_task_dir, tr['unmatched'])
        write_process_log(output_task_dir, task_id, tr)

        logger.info(f'[task:{task_id}] 冲突确认处理完成: {task["message"]}')

    except Exception as e:
        logger.error(f'[task:{task_id}] 冲突确认处理失败: {e}\n{traceback.format_exc()}')
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['message'] = f'冲突确认处理失败: {str(e)}'


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
