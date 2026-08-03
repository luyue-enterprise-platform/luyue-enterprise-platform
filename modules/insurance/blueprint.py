# -*- coding: utf-8 -*-
"""
鲁岳企业服务.重点群体社保批量统计智能核算系统
Flask Blueprint 模块 - 作为综合智能平台的子模块运行

本模块由原始 Flask 应用重构而来，登录/注册/退出由父应用统一处理，
首页（门户）由父应用提供。本 Blueprint 挂载在 /insurance 前缀下。
"""
import os
import sys
import uuid
import json
import shutil
import threading
import time
import logging
import traceback
from datetime import datetime

from flask import (Blueprint, request, jsonify, send_file,
                   render_template, session, redirect, url_for)

# ============ 路径设置 ============
# PyInstaller 单文件模式：模板/静态/核心模块在临时解压目录(sys._MEIPASS)
# 用户数据（数据库/上传/输出/日志）放在可写数据目录
# 自动处理 Program Files 受保护目录：重定向到 %APPDATA%\\鲁岳企业服务
from core.paths import data_dir, resource_dir
DATA_DIR = data_dir()

IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    # PyInstaller 单文件模式：模板/静态资源按 build.spec 中的 (src, dst) 存放
    # build.spec 将 modules/insurance/templates 打包到 modules/insurance/templates
    # 因此本蓝图的 RESOURCE_DIR 应为 sys._MEIPASS/modules/insurance
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'insurance')
else:
    # blueprint.py 位于 modules/insurance/ 目录下
    # RESOURCE_DIR 指向 modules/insurance/（模板、静态资源、core 模块）
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保项目根目录（资源目录）在 Python 路径中，以便导入 core.auth 和 modules.insurance.core.*
# 注意：使用 RESOURCE_DIR（项目根/_MEIPASS）而非 DATA_DIR（可能已重定向到 %APPDATA%）
_PROJECT_ROOT = resource_dir()
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ============ 日志配置 ============
LOG_DIR = os.path.join(DATA_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger('insurance')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(os.path.join(LOG_DIR, 'insurance.log'), encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(_sh)

UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
OUTPUT_DIR = os.path.join(DATA_DIR, 'outputs')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============ 导入保险系统核心模块 ============
from modules.insurance.core.ocr_engine import ocr_to_text, pdf_to_images
from modules.insurance.core.data_parser import parse_ocr_result, group_by_person, extract_company_name
from modules.insurance.core.stats_calculator import calc_all_stats
from modules.insurance.core.excel_generator import generate_excel
from modules.insurance.core.roster_parser import (parse_roster, parse_roster_from_table,
                                                   match_person_to_roster, extract_roster_company_name)
from modules.insurance.core.file_organizer import organize_files, INSURANCE_FOLDER_MAP

# ============ 导入共享认证模块（由父应用提供） ============
from core.auth import (init_db, create_user, verify_user, get_user_count,
                       generate_invite_code, validate_invite_code,
                       consume_invite_code, get_invite_codes,
                       get_all_users, toggle_user_active, delete_user,
                       reset_password, get_user_by_id,
                       login_required, admin_required)

# ============ 创建 Blueprint ============
insurance_bp = Blueprint(
    'insurance',
    __name__,
    url_prefix='/insurance',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static'),
)

# 初始化数据库（SQLite 文件放在 DATA_DIR 下）
init_db(data_dir=os.path.join(DATA_DIR, 'data'))

# ============ 任务状态管理 ============
tasks = {}
tasks_lock = threading.Lock()


def process_task(task_id, file_paths, roster, roster_company='', roster_source_path='', year_range=None):
    """后台线程：PDF转图片 -> 逐张OCR识别 -> 解析 -> 分组 -> 统计 -> 文件整理 -> 生成Excel"""
    try:
        with tasks_lock:
            tasks[task_id]['status'] = 'processing'
            tasks[task_id]['current'] = 0
            tasks[task_id]['message'] = '正在准备文件...'

        logger.info(f'[task:{task_id}] 开始处理 {len(file_paths)} 个文件')

        # 定义任务目录（供后续重传合并使用）
        task_dir = os.path.join(UPLOAD_DIR, task_id)

        # ===== 将PDF转为图片，与普通图片一起展平为待识别列表 =====
        # all_items 元素: (display_name, file_path, source_origin)
        # source_origin 用于文件整理去重：PDF多页视为同一源文件
        all_items = []  # [(display_name, file_path, source_origin), ...]
        ocr_results = []

        pdf_count = sum(1 for fp in file_paths if os.path.splitext(fp)[1].lower() == '.pdf')
        logger.info(f'[task:{task_id}] 其中 {pdf_count} 个PDF文件')

        for idx, fp in enumerate(file_paths):
            # 检查是否已取消
            with tasks_lock:
                if tasks[task_id].get('cancelled'):
                    tasks[task_id]['status'] = 'cancelled'
                    tasks[task_id]['message'] = '任务已取消'
                    logger.info(f'[task:{task_id}] 任务被用户取消（PDF转换阶段）')
                    return

            ext = os.path.splitext(fp)[1].lower()
            if ext == '.pdf':
                with tasks_lock:
                    tasks[task_id]['message'] = f'正在转换PDF ({idx+1}/{pdf_count}): {os.path.basename(fp)}'
                try:
                    page_images = pdf_to_images(fp)
                    pdf_basename = os.path.basename(fp)
                    logger.info(f'[task:{task_id}] PDF {pdf_basename} 转成 {len(page_images)} 页')
                    for img_path in page_images:
                        all_items.append((os.path.basename(img_path), img_path, pdf_basename))
                except Exception as e:
                    err_msg = f'PDF转换失败: {e}'
                    logger.error(f'[task:{task_id}] {err_msg}\n{traceback.format_exc()}')
                    ocr_results.append({
                        'filename': os.path.basename(fp),
                        'error': err_msg,
                        'name': '', 'idcard': '',
                        'insurance_type': None, 'period': None, 'raw_text': ''
                    })
            else:
                img_basename = os.path.basename(fp)
                all_items.append((img_basename, fp, img_basename))

        with tasks_lock:
            tasks[task_id]['total'] = len(all_items)

        logger.info(f'[task:{task_id}] 共需识别 {len(all_items)} 张图片')

        for i, (display_name, fp, source_origin) in enumerate(all_items):
            # 检查是否已取消
            with tasks_lock:
                if tasks[task_id].get('cancelled'):
                    tasks[task_id]['status'] = 'cancelled'
                    tasks[task_id]['message'] = '任务已取消'
                    logger.info(f'[task:{task_id}] 任务被用户取消')
                    return

            # 检查是否已暂停，暂停时循环等待
            while True:
                with tasks_lock:
                    is_paused = tasks[task_id].get('paused')
                    is_cancelled = tasks[task_id].get('cancelled')
                if is_cancelled:
                    with tasks_lock:
                        tasks[task_id]['status'] = 'cancelled'
                        tasks[task_id]['message'] = '任务已取消'
                        tasks[task_id]['paused'] = False
                    logger.info(f'[task:{task_id}] 任务在暂停中被取消')
                    return
                if not is_paused:
                    break
                time.sleep(0.5)

            with tasks_lock:
                tasks[task_id]['current'] = i
                tasks[task_id]['message'] = f'正在识别 ({i+1}/{len(all_items)}): {display_name}'

            try:
                text = ocr_to_text(fp)
                parsed = parse_ocr_result(text)
                parsed['filename'] = display_name
                parsed['_source_path'] = fp  # 保留源文件路径供整理使用
                parsed['_source_origin'] = source_origin  # 原始源文件名，用于PDF多页去重
                ocr_results.append(parsed)
                # 记录OCR解析详情，便于排查问题
                logger.info(
                    f'[task:{task_id}] {display_name} → '
                    f'险种={parsed.get("insurance_type")}, '
                    f'姓名={parsed.get("name")}, '
                    f'时间段={parsed.get("period")}, '
                    f'单位={parsed.get("company_name")}'
                )
            except Exception as e:
                err_msg = str(e)
                logger.error(f'[task:{task_id}] 识别 {display_name} 失败: {err_msg}\n{traceback.format_exc()}')
                ocr_results.append({
                    'filename': display_name,
                    'error': err_msg,
                    'name': '', 'idcard': '',
                    'insurance_type': None, 'period': None, 'raw_text': '',
                    '_source_path': fp,
                    '_source_origin': source_origin
                })

            time.sleep(0.05)

        with tasks_lock:
            tasks[task_id]['current'] = len(all_items)
            tasks[task_id]['message'] = '正在整理文件...'

        # ===== 按花名册重命名 + 按险种分文件夹 =====
        organize_dir = os.path.join(OUTPUT_DIR, task_id, '参保证明')
        os.makedirs(organize_dir, exist_ok=True)
        try:
            organize_result = organize_files(ocr_results, roster, organize_dir)
            org_count = organize_result['organized_count']
            logger.info(f'[task:{task_id}] 文件整理完成: {org_count} 个文件')
        except Exception as e:
            logger.error(f'[task:{task_id}] 文件整理失败: {e}\n{traceback.format_exc()}')
            organize_result = {'organized_count': 0, 'folder_structure': {}, 'unmatched': [], 'no_roster': not roster}

        with tasks_lock:
            tasks[task_id]['message'] = '正在统计计算...'

        # 区分成功和失败的OCR结果
        success_results = []
        failed_results = []
        all_files = []
        for r in ocr_results:
            fn = r.get('filename', '')
            if r.get('error') or (not r.get('name') and not r.get('idcard')):
                failed_results.append({
                    'filename': fn,
                    'error': r.get('error', '未能识别出有效信息'),
                })
            else:
                success_results.append(r)
            all_files.append(fn)

        logger.info(f'[task:{task_id}] OCR完成 — 成功: {len(success_results)}, 失败: {len(failed_results)}')

        # ===== 缴费单位提取与验证 =====
        # 从OCR结果中收集所有缴费单位名称
        ocr_companies = {}
        for r in success_results:
            cn = r.get('company_name', '')
            if cn:
                ocr_companies[cn] = ocr_companies.get(cn, 0) + 1

        # 确定最终的缴费单位：OCR中出现次数最多的
        final_company = ''
        if ocr_companies:
            final_company = max(ocr_companies, key=ocr_companies.get)
            logger.info(f'[task:{task_id}] OCR识别到的缴费单位: {ocr_companies}, 最终选用: {final_company}')

        # 如果花名册中有公司名而OCR没有提取到，使用花名册的公司名
        if not final_company and roster_company:
            final_company = roster_company

        # 验证：如果OCR和花名册都有公司名，选择花名册的公司名作为标准
        # 因为花名册是用户确认过的，更权威
        if roster_company and roster_source_path:
            try:
                roster_file_company = extract_roster_company_name(roster_source_path)
                if roster_file_company:
                    if final_company and final_company != roster_file_company:
                        logger.warning(
                            f'[task:{task_id}] 缴费单位不一致: '
                            f'参保证明="{final_company}", 花名册="{roster_file_company}"'
                        )
                    # 以花名册的公司名为准
                    final_company = roster_file_company
                    logger.info(f'[task:{task_id}] 以花名册公司名为准: {final_company}')
            except Exception:
                pass

        # ===== 关键：过滤掉缴费单位不一致的图片，不计入有效统计 =====
        company_mismatch_files = []
        valid_results = []
        if final_company:
            for r in success_results:
                cn = r.get('company_name', '').strip()
                if cn and cn != final_company:
                    # 缴费单位不一致 → 排除，不计入统计
                    company_mismatch_files.append({
                        'filename': r.get('filename', ''),
                        'ocr_company': cn,
                        'expected_company': final_company,
                    })
                    logger.warning(
                        f'[task:{task_id}] 排除不一致文件: {r.get("filename","")} '
                        f'(缴费单位="{cn}", 期望="{final_company}")'
                    )
                else:
                    # 缴费单位一致（或无公司名信息）→ 保留
                    valid_results.append(r)
        else:
            # 没有识别到任何缴费单位，全部保留（兼容性处理）
            valid_results = list(success_results)

        excluded_count = len(success_results) - len(valid_results)
        logger.info(f'[task:{task_id}] 缴费单位验证: 保留 {len(valid_results)} 条, '
                    f'排除 {excluded_count} 条, 花名册公司="{roster_company}"')

        # 按人员分组（仅用缴费单位验证通过的结果）
        persons = group_by_person(valid_results)
        person_stats, year_cols = calc_all_stats(persons, year_range)

        # 生成Excel
        excel_filename = f'申报重点群体税收优惠政策总台账_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        excel_path = os.path.join(OUTPUT_DIR, excel_filename)
        gen_result = generate_excel(persons, excel_path, roster=roster, company_name=final_company, year_range=year_range)
        logger.info(f'[task:{task_id}] Excel生成完成: {excel_path}')

        # 年度台账独立文件路径
        yearly_ledger_files = gen_result.get('yearly_ledger_files', [])
        for yf in yearly_ledger_files:
            logger.info(f'[task:{task_id}] 年度台账: {yf["filepath"]}')

        # 构建花名册映射（优先按身份证号，回退到姓名）
        roster_map_by_idcard = {}
        roster_map_by_name = {}
        if roster:
            for item in roster:
                idc = item.get('idcard', '').strip()
                nm = item.get('name', '').strip()
                if idc:
                    roster_map_by_idcard[idc.upper()] = item.get('identity_type', '')
                if nm:
                    roster_map_by_name[nm] = item.get('identity_type', '')

        def _get_identity_type(ps):
            """优先按身份证号匹配，回退到姓名"""
            idc = ps.get('idcard', '').strip().upper()
            if idc and idc in roster_map_by_idcard:
                return roster_map_by_idcard[idc]
            nm = ps.get('name', '').strip()
            if nm and nm in roster_map_by_name:
                return roster_map_by_name[nm]
            return ''

        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = '处理完成'
            tasks[task_id]['result'] = {
                'person_stats': [
                    {
                        'name': ps['name'],
                        'idcard': ps['idcard'],
                        'identity_type': _get_identity_type(ps),
                        'insurances': {
                            k: {'start': v[0], 'end': v[1]}
                            for k, v in ps['insurances'].items()
                        },
                        'overlap_start': ps['overlap_start'],
                        'overlap_end': ps['overlap_end'],
                        'overlap_months': ps['overlap_months'],
                        'has_overlap': ps['has_overlap'],
                        'yearly_months': ps['yearly_months'],
                    }
                    for ps in person_stats
                ],
                'year_cols': year_cols,
                'excel_path': excel_path,
                'excel_filename': excel_filename,
                'yearly_ledger_files': yearly_ledger_files,
                'ocr_count': len(ocr_results),
                'person_count': len(persons),
                'success_count': len(valid_results),
                'excluded_count': excluded_count,
                'failed_count': len(failed_results),
                'failed_files': failed_results,
                'all_files': all_files,
                # 每张图片的识别详情（含险种/姓名/身份证/时间段/单位等）
                # 用于前端展示，方便定位"险种未识别"等问题
                'image_details': [
                    {
                        'filename': r.get('filename', ''),
                        'name': r.get('name', ''),
                        'idcard': r.get('idcard', ''),
                        'insurance_type': r.get('insurance_type') or '',
                        'company_name': r.get('company_name', ''),
                        'period': r.get('period', ''),
                        'error': '',
                    }
                    for r in success_results
                ] + [
                    {
                        'filename': r.get('filename', ''),
                        'name': '',
                        'idcard': '',
                        'insurance_type': '',
                        'company_name': '',
                        'period': '',
                        'error': r.get('error', '识别失败'),
                    }
                    for r in failed_results
                ],
                'organize_result': {
                    'organized_count': organize_result['organized_count'],
                    'folder_structure': organize_result['folder_structure'],
                    'unmatched': organize_result['unmatched'],
                    'no_roster': organize_result['no_roster'],
                },
                'organize_dir': organize_dir,
                # 缴费单位信息
                'company_name': final_company,
                'roster_company': roster_company,
                'ocr_companies': ocr_companies,
                'company_mismatch_files': company_mismatch_files,
                # 保存内部数据供重传合并使用
                '_success_results': success_results,
                '_task_dir': task_dir,
                '_year_range': year_range,
            }
        logger.info(f'[task:{task_id}] 处理完成')

    except Exception as e:
        err_msg = f'处理失败: {e}'
        logger.error(f'[task:{task_id}] {err_msg}\n{traceback.format_exc()}')
        with tasks_lock:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = err_msg


# ============ 邀请码管理（管理员） ============
@insurance_bp.route('/api/invite_codes')
@admin_required
def api_invite_codes():
    codes = get_invite_codes()
    return jsonify({'codes': codes})


@insurance_bp.route('/api/generate_invite', methods=['POST'])
@admin_required
def api_generate_invite():
    code = generate_invite_code(session['user_id'])
    return jsonify({'code': code})


# ============ 账号管理（管理员） ============
@insurance_bp.route('/api/users')
@admin_required
def api_users():
    users = get_all_users()
    return jsonify({'users': users})


@insurance_bp.route('/api/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def api_toggle_user(uid):
    # 不能停用自己
    if uid == session['user_id']:
        return jsonify({'error': '不能停用自己的账号'}), 400
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    new_state = not user['is_active']
    toggle_user_active(uid, new_state)
    return jsonify({'is_active': new_state})


@insurance_bp.route('/api/users/<int:uid>/delete', methods=['POST'])
@admin_required
def api_delete_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': '不能删除自己的账号'}), 400
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if user['is_admin']:
        return jsonify({'error': '不能删除管理员账号，请先取消管理员身份或停用'}), 400
    delete_user(uid)
    return jsonify({'ok': True})


@insurance_bp.route('/api/users/<int:uid>/reset_password', methods=['POST'])
@admin_required
def api_reset_password(uid):
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    data = request.get_json(silent=True) or {}
    new_pwd = data.get('password', '')
    if len(new_pwd) < 6:
        return jsonify({'error': '密码至少6位'}), 400
    reset_password(uid, new_pwd)
    return jsonify({'ok': True})


@insurance_bp.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    """当前登录用户修改自己的密码"""
    data = request.get_json(silent=True) or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    if not old_password or not new_password:
        return jsonify({'error': '请输入旧密码和新密码'}), 400
    if len(new_password) < 6:
        return jsonify({'error': '新密码至少6位'}), 400

    user, err = verify_user(session['username'], old_password)
    if err:
        return jsonify({'error': '旧密码错误'}), 400

    reset_password(session['user_id'], new_password)
    logger.info(f'用户 {session["username"]} 修改了密码')
    return jsonify({'ok': True})


# ============ 花名册上传 ============
@insurance_bp.route('/api/roster', methods=['POST'])
@login_required
def upload_roster():
    """上传花名册Excel/CSV表格，解析后返回人员列表"""
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '未选择文件'}), 400

    task_dir = os.path.join(UPLOAD_DIR, f'roster_{uuid.uuid4().hex[:8]}')
    os.makedirs(task_dir, exist_ok=True)

    all_roster = []
    errors = []

    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.xlsx', '.xls', '.csv'):
            errors.append(f'{f.filename}: 不支持的格式，请上传 .xlsx 或 .csv 文件')
            continue
        save_path = os.path.join(task_dir, f.filename)
        f.save(save_path)

        try:
            roster = parse_roster_from_table(save_path)
            all_roster.extend(roster)
        except Exception as e:
            errors.append(f'{f.filename}: {str(e)}')

    # 按序号排序并重新编号
    all_roster.sort(key=lambda x: x.get('seq', 999))
    for i, item in enumerate(all_roster):
        item['seq'] = i + 1

    # 提取花名册中的公司名
    roster_company = ''
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in ('.xlsx', '.xls', '.csv'):
            save_path = os.path.join(task_dir, f.filename)
            try:
                roster_company = extract_roster_company_name(save_path)
                if roster_company:
                    break
            except Exception:
                pass

    # 保存花名册源文件路径（供 process_task 使用）
    roster_source_path = None
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in ('.xlsx', '.xls', '.csv'):
            roster_source_path = os.path.join(task_dir, f.filename)
            break

    return jsonify({
        'roster': all_roster,
        'count': len(all_roster),
        'errors': errors,
        'company_name': roster_company,
        'roster_source_path': roster_source_path,
    })


# ============ 主页面 ============
@insurance_bp.route('/')
@login_required
def index():
    return render_template('insurance_index.html', username=session.get('username'),
                           is_admin=session.get('is_admin', False))


# ============ 上传社保图片 ============
@insurance_bp.route('/api/upload', methods=['POST'])
@login_required
def upload():
    """上传图片/PDF，返回task_id"""
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '未选择文件'}), 400

    # 获取花名册（从表单数据）
    roster_json = request.form.get('roster', '[]')
    try:
        roster = json.loads(roster_json)
    except (json.JSONDecodeError, TypeError):
        roster = []

    # 获取花名册公司名和源文件路径（用于缴费单位验证）
    roster_company = request.form.get('roster_company', '')
    roster_source_path = request.form.get('roster_source_path', '')

    # 获取用户选择的统计年月范围（可选）
    year_start_str = request.form.get('year_start', '')
    month_start_str = request.form.get('month_start', '')
    year_end_str = request.form.get('year_end', '')
    month_end_str = request.form.get('month_end', '')
    year_range = None
    if year_start_str and month_start_str and year_end_str and month_end_str:
        try:
            period_start = f'{int(year_start_str):04d}-{int(month_start_str):02d}'
            period_end = f'{int(year_end_str):04d}-{int(month_end_str):02d}'
            # 验证起始不晚于截止
            sy1, sm1 = int(year_start_str), int(month_start_str)
            ey1, em1 = int(year_end_str), int(month_end_str)
            if (sy1, sm1) <= (ey1, em1):
                year_range = (period_start, period_end)
                logger.info(f'[upload] 用户选择年月范围: {period_start} ~ {period_end}')
        except (ValueError, TypeError):
            pass

    task_id = str(uuid.uuid4())[:8]
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    file_paths = []
    saved_files = []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf'):
            continue
        # 文件夹上传时filename可能含子目录路径，需创建子目录
        save_path = os.path.join(task_dir, f.filename)
        save_dir = os.path.dirname(save_path)
        if save_dir and not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
        f.save(save_path)
        file_paths.append(save_path)
        saved_files.append(f.filename)

    if not file_paths:
        return jsonify({'error': '未找到支持的文件（JPG/PNG/BMP/TIF/PDF）'}), 400

    with tasks_lock:
        tasks[task_id] = {
            'status': 'pending',
            'current': 0,
            'total': len(file_paths),
            'message': '等待处理...',
            'files': saved_files,
            'result': None,
            'created_at': datetime.now().isoformat(),
            'paused': False,
            'cancelled': False,
        }

    t = threading.Thread(target=process_task,
                         args=(task_id, file_paths, roster,
                               roster_company, roster_source_path, year_range),
                         daemon=True)
    t.start()

    return jsonify({'task_id': task_id, 'file_count': len(file_paths)})


@insurance_bp.route('/api/task/<task_id>/pause', methods=['POST'])
@login_required
def pause_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] not in ('processing', 'pending'):
        return jsonify({'error': '当前状态无法暂停'}), 400
    with tasks_lock:
        tasks[task_id]['paused'] = True
    logger.info(f'[task:{task_id}] 用户请求暂停')
    return jsonify({'ok': True})


@insurance_bp.route('/api/task/<task_id>/resume', methods=['POST'])
@login_required
def resume_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    with tasks_lock:
        tasks[task_id]['paused'] = False
    logger.info(f'[task:{task_id}] 用户请求恢复')
    return jsonify({'ok': True})


@insurance_bp.route('/api/task/<task_id>/cancel', methods=['POST'])
@login_required
def cancel_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] in ('done', 'cancelled'):
        return jsonify({'error': '任务已结束'}), 400
    with tasks_lock:
        tasks[task_id]['cancelled'] = True
        tasks[task_id]['paused'] = False  # 解除暂停以便线程退出
    logger.info(f'[task:{task_id}] 用户请求取消')
    return jsonify({'ok': True})


@insurance_bp.route('/api/progress/<task_id>')
@login_required
def progress(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({
        'status': task['status'],
        'current': task['current'],
        'total': task['total'],
        'message': task['message'],
        'paused': task.get('paused', False),
        'cancelled': task.get('cancelled', False),
    })


@insurance_bp.route('/api/result/<task_id>')
@login_required
def result(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成', 'status': task['status']}), 400
    # 过滤掉内部字段（以 _ 开头的）
    res = {k: v for k, v in task['result'].items() if not k.startswith('_')}
    # 年度台账只返回文件名列表，不暴露服务器路径
    if 'yearly_ledger_files' in res:
        res['yearly_ledger_files'] = [f['filename'] for f in res['yearly_ledger_files']]
    return jsonify(res)


@insurance_bp.route('/api/retry/<task_id>', methods=['POST'])
@login_required
def retry_task(task_id):
    """重新上传失败文件，补全识别结果"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成，无法补充上传'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '未选择文件'}), 400

    # 获取当前的成功结果
    old_result = task.get('result', {})
    success_results = old_result.get('_success_results', [])
    task_dir = old_result.get('_task_dir', os.path.join(UPLOAD_DIR, task_id))

    # 获取花名册（从原任务参数中）
    roster_json = request.form.get('roster', '[]')
    try:
        roster = json.loads(roster_json)
    except (json.JSONDecodeError, TypeError):
        roster = []

    if not roster:
        roster = []

    # 保存并OCR新文件
    new_paths = []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf'):
            continue
        save_path = os.path.join(task_dir, f'retry_{uuid.uuid4().hex[:6]}_{f.filename}')
        save_dir = os.path.dirname(save_path)
        if save_dir and not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
        f.save(save_path)
        new_paths.append(save_path)

    if not new_paths:
        return jsonify({'error': '未找到支持的图片文件'}), 400

    logger.info(f'[task:{task_id}] 补充上传 {len(new_paths)} 个文件')

    # OCR新文件
    new_results = []
    new_failed = []
    all_items = []

    for fp in new_paths:
        ext = os.path.splitext(fp)[1].lower()
        if ext == '.pdf':
            try:
                page_images = pdf_to_images(fp)
                pdf_basename = os.path.basename(fp)
                for img_path in page_images:
                    all_items.append((os.path.basename(img_path), img_path, pdf_basename))
            except Exception as e:
                new_failed.append({
                    'filename': os.path.basename(fp),
                    'error': f'PDF转换失败: {e}',
                })
        else:
            img_basename = os.path.basename(fp)
            all_items.append((img_basename, fp, img_basename))

    for display_name, fp, source_origin in all_items:
        try:
            text = ocr_to_text(fp)
            parsed = parse_ocr_result(text)
            parsed['filename'] = display_name
            parsed['_source_path'] = fp
            parsed['_source_origin'] = source_origin
            new_results.append(parsed)
        except Exception as e:
            new_failed.append({
                'filename': display_name,
                'error': str(e),
            })

    # 分类新结果
    retry_success = []
    retry_failed = []
    for r in new_results:
        if r.get('error') or (not r.get('name') and not r.get('idcard')):
            retry_failed.append({
                'filename': r.get('filename', ''),
                'error': r.get('error', '未能识别出有效信息'),
            })
        else:
            retry_success.append(r)

    # 合并：原有成功 + 新成功
    all_success = success_results + retry_success
    all_failed = retry_failed  # 新的失败文件（替换旧的失败列表）

    logger.info(f'[task:{task_id}] 补充识别 — 新成功: {len(retry_success)}, 仍失败: {len(retry_failed)}, 合并后总成功: {len(all_success)}')

    # 重新统计（复用原始年度范围）
    year_range = old_result.get('_year_range', None)
    persons = group_by_person(all_success)
    person_stats, year_cols = calc_all_stats(persons, year_range)

    # 重新生成Excel（保留补传前识别到的缴费单位）
    company_name = old_result.get('company_name', '')
    excel_filename = f'申报重点群体税收优惠政策总台账_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    excel_path = os.path.join(OUTPUT_DIR, excel_filename)
    gen_result = generate_excel(persons, excel_path, roster=roster, company_name=company_name, year_range=year_range)
    yearly_ledger_files = gen_result.get('yearly_ledger_files', [])

    roster_map = {}
    if roster:
        for item in roster:
            nm = item.get('name', '').strip()
            if nm:
                roster_map[nm] = item.get('identity_type', '')

    # 重新整理文件
    organize_dir = os.path.join(OUTPUT_DIR, task_id, '参保证明')
    os.makedirs(organize_dir, exist_ok=True)
    try:
        organize_result = organize_files(all_success, roster, organize_dir)
    except Exception:
        organize_result = {'organized_count': 0, 'folder_structure': {}, 'unmatched': [], 'no_roster': not roster}

    total_ocr = len(success_results) + len(new_results)
    all_files = (old_result.get('all_files', []) +
                 [os.path.basename(fp) for fp in new_paths])

    with tasks_lock:
        tasks[task_id]['result'] = {
            'person_stats': [
                {
                    'name': ps['name'],
                    'idcard': ps['idcard'],
                    'identity_type': roster_map.get(ps['name'], ''),
                    'insurances': {
                        k: {'start': v[0], 'end': v[1]}
                        for k, v in ps['insurances'].items()
                    },
                    'overlap_start': ps['overlap_start'],
                    'overlap_end': ps['overlap_end'],
                    'overlap_months': ps['overlap_months'],
                    'has_overlap': ps['has_overlap'],
                    'yearly_months': ps['yearly_months'],
                }
                for ps in person_stats
            ],
            'year_cols': year_cols,
            'excel_path': excel_path,
            'excel_filename': excel_filename,
            'yearly_ledger_files': yearly_ledger_files,
            'ocr_count': total_ocr,
            'person_count': len(persons),
            'success_count': len(all_success),
            'failed_count': len(all_failed),
            'failed_files': all_failed,
            'all_files': all_files,
            'organize_result': {
                'organized_count': organize_result['organized_count'],
                'folder_structure': organize_result['folder_structure'],
                'unmatched': organize_result['unmatched'],
                'no_roster': organize_result['no_roster'],
            },
            'organize_dir': organize_dir,
            'company_name': company_name,
            'roster_company': old_result.get('roster_company', ''),
            'ocr_companies': old_result.get('ocr_companies', {}),
            'company_mismatch_files': old_result.get('company_mismatch_files', []),
            # 每张图片的识别详情（含失败信息）
            'image_details': [
                {
                    'filename': r.get('filename', ''),
                    'name': r.get('name', ''),
                    'idcard': r.get('idcard', ''),
                    'insurance_type': r.get('insurance_type') or '',
                    'company_name': r.get('company_name', ''),
                    'period': r.get('period', ''),
                    'error': '',
                }
                for r in all_success
            ] + [
                {
                    'filename': r.get('filename', ''),
                    'name': '',
                    'idcard': '',
                    'insurance_type': '',
                    'company_name': '',
                    'period': '',
                    'error': r.get('error', '识别失败'),
                }
                for r in all_failed
            ],
            '_success_results': all_success,
            '_task_dir': task_dir,
            '_year_range': year_range,
        }
    logger.info(f'[task:{task_id}] 补充识别完成')

    # 过滤内部字段返回
    res = {k: v for k, v in tasks[task_id]['result'].items() if not k.startswith('_')}
    if 'yearly_ledger_files' in res:
        res['yearly_ledger_files'] = [f['filename'] for f in res['yearly_ledger_files']]
    return jsonify(res)


@insurance_bp.route('/api/download/<task_id>')
@login_required
def download(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404
    excel_path = task['result']['excel_path']
    if not os.path.exists(excel_path):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(excel_path, as_attachment=True,
                     download_name=task['result']['excel_filename'])


@insurance_bp.route('/api/download_yearly/<task_id>/<filename>')
@login_required
def download_yearly(task_id, filename):
    """下载年度台账独立Excel文件"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404
    yearly_files = task['result'].get('yearly_ledger_files', [])
    for yf in yearly_files:
        if yf.get('filename') == filename:
            yf_path = yf.get('filepath', '')
            if os.path.exists(yf_path):
                return send_file(yf_path, as_attachment=True,
                                 download_name=filename)
            break
    return jsonify({'error': '文件不存在'}), 404


@insurance_bp.route('/api/download_organized/<task_id>')
@login_required
def download_organized(task_id):
    """下载整理后的文件夹（zip）"""
    import zipfile
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    organize_dir = task['result'].get('organize_dir')
    if not organize_dir or not os.path.exists(organize_dir):
        return jsonify({'error': '整理文件不存在'}), 404

    # 打包为zip
    zip_path = organize_dir + '.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(organize_dir):
            for fn in files:
                fp = os.path.join(root, fn)
                arcname = os.path.relpath(fp, organize_dir)
                zf.write(fp, arcname)

    return send_file(zip_path, as_attachment=True,
                     download_name=f'参保证明_{task_id}.zip')


@insurance_bp.route('/api/save_to/<task_id>', methods=['POST'])
@login_required
def save_to(task_id):
    """弹出系统原生文件夹选择对话框，将文件保存到用户选择的位置
    file_type: table=仅表格, organized=仅参保证明, all=全部
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    result = task['result']
    excel_path = result.get('excel_path')
    organize_dir = result.get('organize_dir')

    # 读取 file_type 参数
    data = request.get_json(silent=True) or {}
    file_type = data.get('file_type', 'all')

    # 根据file_type设置对话框标题
    if file_type == 'table':
        dialog_title = '选择保存位置（总台账和年度台账将保存到此文件夹）'
    elif file_type == 'organized':
        dialog_title = '选择保存位置（参保证明将保存到此文件夹）'
    else:
        dialog_title = '选择保存位置（总台账、年度台账和参保证明将保存到此文件夹）'

    # 使用 tkinter 弹出系统原生文件夹选择对话框
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        save_dir = filedialog.askdirectory(
            title=dialog_title,
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'保存对话框异常: {e}')
        return jsonify({'error': f'无法打开保存对话框: {e}'}), 500

    if not save_dir:
        return jsonify({'cancelled': True})

    saved_files = []

    # 复制总台账 + 年度台账（file_type=table 或 all）
    if file_type in ('table', 'all'):
        # 复制总台账 Excel 文件
        if excel_path and os.path.exists(excel_path):
            dest_excel = os.path.join(save_dir, os.path.basename(excel_path))
            shutil.copy2(excel_path, dest_excel)
            saved_files.append(os.path.basename(excel_path))
            logger.info(f'[task:{task_id}] Excel已保存到: {dest_excel}')

        # 复制年度台账 Excel 文件（放到"年度台账"子文件夹中）
        yearly_files = result.get('yearly_ledger_files', [])
        if yearly_files:
            yearly_dir = os.path.join(save_dir, '年度台账')
            os.makedirs(yearly_dir, exist_ok=True)
            for yf in yearly_files:
                yf_path = yf.get('filepath', '')
                if yf_path and os.path.exists(yf_path):
                    dest_yf = os.path.join(yearly_dir, os.path.basename(yf_path))
                    shutil.copy2(yf_path, dest_yf)
                    saved_files.append(f'年度台账/{os.path.basename(yf_path)}')
                    logger.info(f'[task:{task_id}] 年度台账已保存到: {dest_yf}')

    # 复制参保证明文件夹（file_type=organized 或 all）
    if file_type in ('organized', 'all'):
        if organize_dir and os.path.exists(organize_dir):
            dest_organize = os.path.join(save_dir, '参保证明')
            if os.path.exists(dest_organize):
                shutil.rmtree(dest_organize)
            shutil.copytree(organize_dir, dest_organize)
            saved_files.append('参保证明/')
            logger.info(f'[task:{task_id}] 参保证明已保存到: {dest_organize}')

    return jsonify({
        'ok': True,
        'save_dir': save_dir,
        'saved_files': saved_files,
    })
