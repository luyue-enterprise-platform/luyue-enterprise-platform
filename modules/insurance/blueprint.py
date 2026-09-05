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
# 用户数据（数据库/上传/输出/日志）放在 exe 同目录
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    # PyInstaller 单文件模式：模板/静态资源按 build.spec 中的 (src, dst) 存放
    # build.spec 将 modules/insurance/templates 打包到 modules/insurance/templates
    # 因此本蓝图的 RESOURCE_DIR 应为 sys._MEIPASS/modules/insurance
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'insurance')
    DATA_DIR = os.path.dirname(sys.executable)  # 用户数据目录
else:
    # blueprint.py 位于 modules/insurance/ 目录下
    # RESOURCE_DIR 指向 modules/insurance/（模板、静态资源、core 模块）
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    # DATA_DIR 指向项目根目录（用户数据目录：uploads/outputs/logs/data）
    DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 确保项目根目录在 Python 路径中，以便导入 core.auth 和 modules.insurance.core.*
_PROJECT_ROOT = DATA_DIR
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


def _get_identity_type(person, roster_index):
    """
    在花名册中查找人员的身份类型（先按身份证号，再按姓名）

    Args:
        person: dict {'name', 'idcard', ...}
        roster_index: 花名册索引

    Returns:
        str: 身份类型，未找到返回空字符串
    """
    idcard = person.get('idcard', '').strip()
    name = person.get('name', '').strip()

    # 1. 优先按身份证号
    if idcard and roster_index.get('idcard_to_entry', {}).get(idcard):
        return roster_index['idcard_to_entry'][idcard].get('identity_type', '')

    # 2. 兜底按姓名
    if name and roster_index.get('name_to_entries', {}).get(name):
        entries = roster_index['name_to_entries'][name]
        if entries:
            return entries[0].get('identity_type', '')

    return ''


def _get_contract_display(person, roster_index):
    """
    在花名册中查找人员的劳动合同起止时间展示文本（v1.1.54，先按身份证号，再按姓名）

    Returns:
        str: 合同起止时间原文，未匹配或无合同信息返回 '-'
    """
    idcard = person.get('idcard', '').strip()
    name = person.get('name', '').strip()

    entry = None
    if idcard and roster_index.get('idcard_to_entry', {}).get(idcard):
        entry = roster_index['idcard_to_entry'][idcard]
    elif name and roster_index.get('name_to_entries', {}).get(name):
        entries = roster_index['name_to_entries'][name]
        if entries:
            entry = entries[0]

    return contract_display_text(entry)

# ============ 导入保险系统核心模块 ============
from modules.insurance.core.ocr_engine import pdf_to_images
from modules.insurance.core.data_parser import parse_ocr_result, parse_ocr_result_from_image, group_by_person, extract_company_name
from modules.insurance.core.stats_calculator import calc_all_stats, get_overlap_years, apply_stat_range_clamp
from modules.insurance.core.contract_overlap import apply_contract_to_stats, contract_display_text
from modules.insurance.core.excel_generator import generate_excel
from modules.insurance.core.roster_parser import (parse_roster, parse_roster_from_table,
                                                   match_person_to_roster, extract_roster_company_name)
from modules.insurance.core.file_organizer import organize_files, INSURANCE_FOLDER_MAP, _validate_idcard

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

# ============ 文件夹选择临时存储 ============
picked_folders = {}  # {pick_id: {'file_paths': [path, ...], 'files': [...]}}

# 四险类型（单位一致性校验/手动补录/时间段修改的合法险种范围）
INSURANCE_TYPES = ('养老保险', '失业保险', '医疗保险', '工伤保险')


def _valid_ym(s):
    """校验 YYYY-MM 格式年月字符串，合法返回规范化的字符串，否则 None"""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) == 7 and s[4] == '-' and s[:4].isdigit() and s[5:].isdigit():
        y, m = int(s[:4]), int(s[5:7])
        if 1 <= m <= 12:
            return f'{y:04d}-{m:02d}'
    return None


def _fmt_period(period):
    """时间段 (start, end) 转为可读字符串"""
    if isinstance(period, (tuple, list)) and len(period) == 2:
        return f'{period[0]} ~ {period[1]}'
    return str(period) if period else ''


def _reset_dir(path):
    """清空并重建目录（防旧整理文件残留），失败容忍不中断"""
    try:
        if os.path.exists(path):
            shutil.rmtree(path)
    except Exception as e:
        logger.warning(f'清空目录失败(忽略): {path}: {e}')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logger.error(f'创建目录失败: {path}: {e}')


def _build_roster_index(roster):
    """构建花名册索引：身份证号优先，姓名兜底"""
    roster_index = {
        'idcard_to_entry': {},
        'name_to_entries': {},
        'order_idcard': {},
        'order_name': {},
    }
    if roster:
        for idx, item in enumerate(roster):
            nm = item.get('name', '').strip()
            idc = item.get('idcard', '').strip()
            if idc:
                if idc not in roster_index['idcard_to_entry']:
                    roster_index['idcard_to_entry'][idc] = item
                    roster_index['order_idcard'][idc] = idx
            if nm:
                roster_index['name_to_entries'].setdefault(nm, []).append(item)
                if nm not in roster_index['order_name']:
                    roster_index['order_name'][nm] = idx
    return roster_index


def _roster_complete(persons, roster):
    """花名册补全：无参保证明人员姓名保留、时间段留空（v1.1.40 重名支持）

    身份证号统一以花名册为准；同名多身份证号按号区分，缺号按姓名顺序占用。
    返回新增人数。
    """
    if not roster:
        return 0
    persons_by_name = {}
    for p in persons:
        persons_by_name.setdefault(p['name'], []).append(p)
    roster_added = 0
    for item in roster:
        r_name = item.get('name', '').strip()
        if not r_name:
            continue
        r_idcard = item.get('idcard', '').strip()
        cands = persons_by_name.get(r_name, [])
        matched = None
        if r_idcard:
            # 优先身份证号精确匹配
            for p in cands:
                if p['idcard'] == r_idcard:
                    matched = p
                    break
            # 其次取同姓名且身份证号为空的未占用人员
            if matched is None:
                for p in cands:
                    if not p['idcard'] and not p.get('_roster_matched'):
                        matched = p
                        break
        else:
            # 花名册无身份证号：按姓名取第一个未占用人员
            for p in cands:
                if not p.get('_roster_matched'):
                    matched = p
                    break
        if matched is not None:
            if r_idcard:
                matched['idcard'] = r_idcard
            matched['_roster_matched'] = True
        else:
            new_p = {
                'name': r_name,
                'idcard': r_idcard,
                'insurances': {},
                '_roster_matched': True,
            }
            persons.append(new_p)
            persons_by_name.setdefault(r_name, []).append(new_p)
            roster_added += 1
    # 清理内部标记
    for p in persons:
        p.pop('_roster_matched', None)
    return roster_added


def _apply_period_overrides(persons, overrides):
    """应用时间段覆盖层（v1.1.43 手动修改/新增的值优先于OCR识别值）

    overrides: {(name, idcard): {险种: (start, end)}}
    v1.1.48 兼容：api_update_period 实际写入的是 'name|idcard' 字符串键
    （v1.1.43~47 键型不一致导致重建 500，"修改时间段"一直不可用——
    测试当时绕过端点直写元组键，漏网），此处两种键型都支持。
    """
    for key, ins_map in overrides.items():
        if isinstance(key, str):
            p_name, _, p_idcard = key.partition('|')
        else:
            p_name, p_idcard = key
        for p in persons:
            if p['name'] == p_name and (not p_idcard or not p['idcard'] or p['idcard'] == p_idcard):
                for ins_type, (start, end) in ins_map.items():
                    p['insurances'][ins_type] = (start, end)
                if p_idcard and not p['idcard']:
                    p['idcard'] = p_idcard
                break


def _rebuild_result(task_id):
    """统一重建：分组 → 花名册补全 → 应用覆盖层 → 统计 → Excel → 文件整理 → 组装result

    v1.1.43 核心函数。任务的内部状态全部存于 tasks[task_id]['result'] 的下划线字段：
      _success_results   有效记录（OCR成功且单位一致，含手动补录记录 _manual=True）
      _excluded_results  缴费单位不一致被排除的记录（带 _excluded 标记）
      _failed_results    识别失败记录（含 _source_path，供手动补录定位原文件）
      _period_overrides  {(name, idcard): {险种: (start, end)}} 手动覆盖层
      _manual_log        操作记录（手动补录/修改时间段/恢复识别值）
    重建完成后返回过滤内部字段后的公开 result dict；任务不存在返回 None。
    """
    with tasks_lock:
        task = tasks.get(task_id)
        if not task or not task.get('result'):
            return None
        inner = task['result']

    # 读取内部状态（浅拷贝列表，重记录引用即可，本函数不修改单条记录）
    success_results = list(inner.get('_success_results', []))
    excluded_results = list(inner.get('_excluded_results', []))
    failed_results = list(inner.get('_failed_results', []))
    roster = inner.get('_roster', [])
    year_range = inner.get('_year_range')
    company_name = inner.get('_company_name', '')
    ocr_companies = inner.get('_ocr_companies', {})
    company_mismatch_files = inner.get('_company_mismatch_files', [])
    all_files = inner.get('_all_files', [])
    task_dir = inner.get('_task_dir', os.path.join(UPLOAD_DIR, task_id))
    roster_company = inner.get('_roster_company', '')
    roster_source_path = inner.get('_roster_source_path', '')
    overrides = inner.get('_period_overrides', {})
    manual_log = inner.get('_manual_log', [])

    # 1) 按人员分组 + 花名册补全
    persons = group_by_person(success_results)
    roster_added = _roster_complete(persons, roster)
    if roster_added:
        logger.info(f'[task:{task_id}] 花名册补全: 新增 {roster_added} 名无参保证明人员（时间段留空）')

    # 2) 应用手动时间段覆盖层（优先于OCR识别值）
    if overrides:
        _apply_period_overrides(persons, overrides)

    # 3) 统计 + 合同叠加比对（v1.1.53）+ 生成Excel
    person_stats, year_cols = calc_all_stats(persons, year_range)
    # v1.1.53：劳动合同起止时间 ∩ 四险重叠参保段 = 有效参保期。
    # 仅裁剪重叠结果层（各险种原始时间段不变）；合同缺失/异常不裁剪、仅生成提示。
    contract_notes = apply_contract_to_stats(person_stats, roster, year_range=year_range)
    # v1.1.55 需求1：统计结果起点与统计时间段对齐——
    # 起点 = max(统计开始, 重叠起点)，终点保持重叠实际结束不裁剪（合同叠加后统一钳制）
    apply_stat_range_clamp(person_stats, year_range)
    # 钳制/合同叠加后按最终重叠层重算年度列
    year_cols = get_overlap_years(
        [ps for ps in person_stats if ps['has_overlap']], year_range=year_range)
    if contract_notes:
        logger.info(f'[task:{task_id}] 合同比对提示 {len(contract_notes)} 条，年度列重算: {year_cols}')
    excel_filename = f'申报重点群体税收优惠政策总台账_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    excel_path = os.path.join(OUTPUT_DIR, excel_filename)
    gen_result = generate_excel(persons, excel_path, roster=roster,
                                company_name=company_name, year_range=year_range,
                                stats=(person_stats, year_cols))
    yearly_ledger_files = gen_result.get('yearly_ledger_files', [])
    logger.info(f'[task:{task_id}] Excel重建完成: {excel_path}')

    # 4) 文件整理（目录清空重建，防止旧文件残留；手动补录记录同时参与重命名归类）
    roster_index = _build_roster_index(roster)
    organize_dir = os.path.join(OUTPUT_DIR, task_id, '参保证明')
    _reset_dir(organize_dir)
    all_for_organize = (list(success_results) + list(excluded_results) +
                        [r for r in failed_results if r.get('_source_path')])
    try:
        organize_result = organize_files(all_for_organize, roster, organize_dir)
        logger.info(f'[task:{task_id}] 文件整理重建: 正常 {organize_result["organized_count"]} 个, '
                    f'异常 {organize_result.get("abnormal_count", 0)} 个')
    except Exception as e:
        logger.error(f'[task:{task_id}] 文件整理失败: {e}\n{traceback.format_exc()}')
        organize_result = {'organized_count': 0, 'folder_structure': {}, 'unmatched': [],
                           'no_roster': not roster, 'abnormal_count': 0}

    # 5) 操作记录持久化（outputs/<task_id>/操作记录.json）
    if manual_log:
        try:
            log_path = os.path.join(OUTPUT_DIR, task_id, '操作记录.json')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(manual_log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f'[task:{task_id}] 操作记录写入失败: {e}')

    # 6) 组装完整 result（公开字段 + 内部状态）
    def _det(rec, error_text=''):
        return {
            'filename': rec.get('filename', ''),
            'name': rec.get('name', ''),
            'idcard': rec.get('idcard', ''),
            'insurance_type': rec.get('insurance_type') or '',
            'company_name': rec.get('company_name', ''),
            'period': rec.get('period', ''),
            'error': error_text,
            'is_manual': bool(rec.get('_manual')),
            'remark': rec.get('_remark', ''),
            'manual_name': rec.get('_manual_name', ''),
        }

    new_result = {
        'person_stats': [
            {
                'name': ps['name'],
                'idcard': ps['idcard'],
                'identity_type': _get_identity_type(ps, roster_index),
                'contract': _get_contract_display(ps, roster_index),
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
        'ocr_count': len(success_results) + len(excluded_results) + len(failed_results),
        'person_count': len(persons),
        'success_count': len(success_results),
        'excluded_count': len(excluded_results),
        'failed_count': len(failed_results),
        'failed_files': [
            {'filename': r.get('filename', ''), 'error': r.get('error', '识别失败')}
            for r in failed_results
        ],
        'all_files': all_files,
        # 每张图片的识别详情（有效 + 单位不一致排除 + 识别失败）
        'image_details': (
            [_det(r) for r in success_results] +
            [_det(r, '缴费单位不一致（已排除）') for r in excluded_results] +
            [_det(r, r.get('error', '识别失败')) for r in failed_results]
        ),
        'organize_result': {
            'organized_count': organize_result['organized_count'],
            'folder_structure': organize_result['folder_structure'],
            'unmatched': organize_result['unmatched'],
            'no_roster': organize_result['no_roster'],
            'abnormal_count': organize_result.get('abnormal_count', 0),
        },
        'organize_dir': organize_dir,
        'company_name': company_name,
        'roster_company': roster_company,
        'ocr_companies': ocr_companies,
        'company_mismatch_files': company_mismatch_files,
        # 操作记录（手动补录/修改时间段/恢复识别值 + v1.1.53 合同比对提示；提示不落盘，每次重建重新生成）
        'operation_log': manual_log + contract_notes,
        # ===== 内部状态（不返回前端） =====
        '_success_results': success_results,
        '_excluded_results': excluded_results,
        '_failed_results': failed_results,
        '_all_files': all_files,
        '_task_dir': task_dir,
        '_year_range': year_range,
        '_roster': roster,
        '_roster_company': roster_company,
        '_roster_source_path': roster_source_path,
        '_company_name': company_name,
        '_ocr_companies': ocr_companies,
        '_company_mismatch_files': company_mismatch_files,
        '_period_overrides': overrides,
        '_manual_log': manual_log,
    }

    with tasks_lock:
        tasks[task_id]['result'] = new_result

    return {k: v for k, v in new_result.items() if not k.startswith('_')}


def _split_ocr_results(task_id, ocr_results, roster):
    """区分成功和失败的OCR结果，并做姓名缺失回填（v1.1.47）

    v1.1.43: 失败记录保留完整信息（含 _source_path），供手动补录定位原文件
    v1.1.47: 姓名缺失但身份证号有效 → 先按身份证号从花名册回填姓名；
             仍无姓名的归入失败桶（原先进成功桶但分组时被静默丢弃，用户看不到）
    """
    roster_index = _build_roster_index(roster or [])
    success_results = []
    failed_results = []
    all_files = []
    for r in ocr_results:
        fn = r.get('filename', '')
        # 姓名缺失但身份证号有效：按花名册回填姓名
        if not r.get('error') and not r.get('name') and r.get('idcard'):
            entry = roster_index['idcard_to_entry'].get(r['idcard'])
            if entry and entry.get('name'):
                r['name'] = entry['name']
                logger.info(f'[task:{task_id}] 姓名缺失，按花名册回填: '
                            f'{r["idcard"]} → {entry["name"]} ({fn})')
        if r.get('error'):
            failed_results.append(r)
        elif not r.get('name'):
            r['error'] = '姓名未识别（可手动补录）'
            failed_results.append(r)
        else:
            success_results.append(r)
        all_files.append(fn)
    return success_results, failed_results, all_files


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
                parsed = parse_ocr_result_from_image(fp)
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
            tasks[task_id]['message'] = '正在分析识别结果...'

        # 区分成功和失败的OCR结果（v1.1.47: 含姓名缺失花名册回填 + 失败桶兜底）
        success_results, failed_results, all_files = _split_ocr_results(
            task_id, ocr_results, roster)

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
        # v1.1.35: 单位一致性校验对【养老保险、失业保险、医疗保险、工伤保险】四险生效
        # （现缴费单位去括号后比对 final_company）；其他险种不在此列
        company_mismatch_files = []
        valid_results = []
        if final_company:
            for r in success_results:
                cn = r.get('company_name', '').strip()
                need_check = (r.get('insurance_type') in ('养老保险', '失业保险', '医疗保险', '工伤保险'))
                if need_check and cn and cn != final_company:
                    # 四险缴费单位不一致 → 排除，不计入统计
                    company_mismatch_files.append({
                        'filename': r.get('filename', ''),
                        'ocr_company': cn,
                        'expected_company': final_company,
                    })
                    logger.warning(
                        f'[task:{task_id}] 排除不一致文件(四险): {r.get("filename","")} '
                        f'(缴费单位="{cn}", 期望="{final_company}")'
                    )
                else:
                    # 单位一致（或无公司名信息）→ 保留
                    valid_results.append(r)
        else:
            # 没有识别到任何缴费单位，全部保留（兼容性处理）
            valid_results = list(success_results)

        excluded_count = len(success_results) - len(valid_results)
        logger.info(f'[task:{task_id}] 缴费单位验证: 保留 {len(valid_results)} 条, '
                    f'排除 {excluded_count} 条, 花名册公司="{roster_company}"')

        # ===== 标记被排除的记录（缴费单位不一致），单独存放供文件整理归入异常图片 =====
        valid_filenames = {r.get('filename', '') for r in valid_results}
        excluded_results = []
        for r in success_results:
            if r.get('filename', '') not in valid_filenames:
                r['_excluded'] = True
                excluded_results.append(r)
                logger.info(f'[task:{task_id}] 标记排除到异常图片: {r.get("filename", "")}')

        # ===== v1.1.43: 写入内部状态，统一重建（统计/Excel/文件整理全链路） =====
        # _rebuild_result 会读取这些内部字段并重建完整 result（含手动补录/时间段覆盖支持）
        with tasks_lock:
            tasks[task_id]['message'] = '正在统计计算与整理文件...'
            tasks[task_id]['result'] = {
                '_success_results': valid_results,
                '_excluded_results': excluded_results,
                '_failed_results': failed_results,
                '_all_files': all_files,
                '_task_dir': task_dir,
                '_year_range': year_range,
                '_roster': roster,
                '_roster_company': roster_company,
                '_roster_source_path': roster_source_path or '',
                '_company_name': final_company,
                '_ocr_companies': ocr_companies,
                '_company_mismatch_files': company_mismatch_files,
                '_period_overrides': {},
                '_manual_log': [],
            }

        _rebuild_result(task_id)

        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = '处理完成'
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

    # v1.1.34: 严格保持花名册原始顺序与原始序号，不排序、不重新编号
    # （多文件合并时按上传顺序拼接，序号用花名册原始值）

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


# ============ 文件夹选择（原生对话框） ============
@insurance_bp.route('/api/pick_folder', methods=['POST'])
@login_required
def api_pick_folder():
    """弹出系统原生文件夹选择对话框，递归扫描文件夹中的图片/PDF文件"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        folder_path = filedialog.askdirectory(
            title='选择社保参保证明文件夹（可包含子文件夹）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件夹选择对话框异常: {e}')
        return jsonify({'error': f'无法打开文件夹选择对话框: {e}'}), 500

    if not folder_path:
        return jsonify({'cancelled': True})

    # 递归遍历文件夹，查找所有图片和PDF文件
    valid_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf'}
    file_list = []
    file_paths = []

    picked_folder_name = os.path.basename(folder_path)

    for dirpath, dirnames, filenames in os.walk(folder_path):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in valid_exts:
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                file_size = os.path.getsize(full_path)
            except Exception:
                continue
            file_paths.append(full_path)
            file_list.append({
                'name': fname,
                'size': file_size,
            })

    if not file_list:
        return jsonify({'error': '所选文件夹中没有找到图片或PDF文件（支持 .jpg .png .bmp .tif .pdf）'})

    # 存储选中文件信息
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


# ============ 上传社保图片 ============
@insurance_bp.route('/api/upload', methods=['POST'])
@login_required
def upload():
    """上传图片/PDF，返回task_id
    支持混合模式：同时上传文件 + 多个文件夹选择(pick_ids)
    """
    # 获取上传的文件
    files = request.files.getlist('files')
    has_uploaded_files = files and not (len(files) == 1 and files[0].filename == '')

    # 获取文件夹选择的 pick_ids（逗号分隔，支持多个文件夹）
    pick_ids_str = request.form.get('pick_ids', '')
    pick_ids = [pid.strip() for pid in pick_ids_str.split(',') if pid.strip()] if pick_ids_str else []

    if not has_uploaded_files and not pick_ids:
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

    # 1. 保存上传的文件
    if has_uploaded_files:
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

    # 2. 从 picked_folders 复制文件（支持多个 pick_id）
    for pick_id in pick_ids:
        with tasks_lock:
            picked = picked_folders.get(pick_id)

        if not picked:
            logger.warning(f'[upload] pick_id {pick_id} 已过期，跳过')
            continue

        for idx, src_path in enumerate(picked['file_paths']):
            safe_name = os.path.basename(src_path)
            dest_path = os.path.join(task_dir, safe_name)
            if os.path.exists(dest_path):
                dest_path = os.path.join(task_dir, f'p{pick_id[:4]}_{idx}_{safe_name}')
            try:
                shutil.copy2(src_path, dest_path)
            except Exception as e:
                logger.error(f'复制文件失败: {src_path} -> {dest_path}: {e}')
                continue
            file_paths.append(dest_path)
            saved_files.append(safe_name)

        # 清理 picked_folders 中的临时数据
        with tasks_lock:
            picked_folders.pop(pick_id, None)

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


# ============ v1.1.43: 识别失败图片手动补录 ============
@insurance_bp.route('/api/manual_fill/<task_id>', methods=['POST'])
@login_required
def api_manual_fill(task_id):
    """手动补录识别失败图片的参保证明信息

    入参 JSON: {filename, name, idcard, insurance_type, start, end}
    - filename: 失败记录的文件名（image_details 中 error 非空行的 filename）
    - name: 姓名（必填，需与花名册一致才能自动重命名归类）
    - idcard: 身份证号（选填，填写则做格式校验）
    - insurance_type: 四险之一
    - start/end: YYYY-MM 起止年月（必填，start <= end）

    补录记录并入有效列表（与自动识别同等效力），原图片文件按花名册重命名归类，
    从异常图片文件夹移出；统计/Excel/文件整理即时重建。
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成'}), 400

    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    name = (data.get('name') or '').strip()
    idcard = (data.get('idcard') or '').strip()
    insurance_type = (data.get('insurance_type') or '').strip()
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()

    # ===== 参数校验 =====
    if not filename:
        return jsonify({'error': '缺少文件名参数'}), 400
    if not name:
        return jsonify({'error': '请输入姓名'}), 400
    if insurance_type not in INSURANCE_TYPES:
        return jsonify({'error': '险种必须为养老/医疗/工伤/失业保险之一'}), 400
    if idcard and not _validate_idcard(idcard):
        return jsonify({'error': '身份证号格式不正确，请核对（15位或18位）'}), 400
    start_ym = _valid_ym(start)
    end_ym = _valid_ym(end)
    if not start_ym or not end_ym:
        return jsonify({'error': '起始/截止年月格式应为 YYYY-MM，如 2023-01'}), 400
    if start_ym > end_ym:
        return jsonify({'error': '起始年月不能晚于截止年月'}), 400

    with tasks_lock:
        result = tasks[task_id]['result']
        failed_results = result.get('_failed_results', [])
        # 按文件名找到待补录的失败记录（取第一条匹配）
        src_rec = None
        for r in failed_results:
            if r.get('filename') == filename:
                src_rec = r
                break
        if src_rec is None:
            return jsonify({'error': '未找到该失败记录（可能已被补录或重传覆盖）'}), 404

        # 从失败列表移除，生成手动记录并入有效列表（同等效力参与统计）
        failed_results.remove(src_rec)
        new_rec = dict(src_rec)
        new_rec.update({
            'name': name,
            'idcard': idcard,
            'insurance_type': insurance_type,
            'period': (start_ym, end_ym),
            'company_name': '',
            'error': None,
            'raw_text': '手动补录',
            '_manual': True,
        })
        new_rec.pop('_excluded', None)
        result['_success_results'].append(new_rec)

        # 操作记录（可追溯）
        result.setdefault('_manual_log', []).append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '手动补录',
            'name': name,
            'idcard': idcard,
            'insurance_type': insurance_type,
            'old': '识别失败',
            'new': f'{start_ym} ~ {end_ym}',
            'operator': session.get('username', ''),
        })

    logger.info(f'[task:{task_id}] 手动补录: {filename} → {name}/{insurance_type} '
                f'{start_ym}~{end_ym} (操作人: {session.get("username", "")})')

    # 统一重建（统计/Excel/文件整理全链路同步，补录图片按花名册重命名归类）
    res = _rebuild_result(task_id)
    if res is None:
        return jsonify({'error': '重建结果失败'}), 500
    if 'yearly_ledger_files' in res:
        res['yearly_ledger_files'] = [f['filename'] for f in res['yearly_ledger_files']]
    return jsonify(res)


# ============ v1.1.43: 时间段手动修改/新增/恢复 ============
@insurance_bp.route('/api/update_period/<task_id>', methods=['POST'])
@login_required
def api_update_period(task_id):
    """手动修改/新增某人的参保证明时间段（即时保存并同步更新统计）

    入参 JSON: {name, idcard, periods: [{insurance_type, start, end}, ...]}
    - start/end 均非空 → 设置该险种的覆盖值（优先于OCR识别值，统计/Excel同步生效）
    - start/end 均为空 → 清除覆盖值，恢复OCR识别结果
    所有操作写入操作记录（operation_log），持久化到 outputs/<task_id>/操作记录.json
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成'}), 400

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    idcard = (data.get('idcard') or '').strip()
    periods = data.get('periods') or []

    if not name:
        return jsonify({'error': '缺少姓名参数'}), 400
    if not isinstance(periods, list) or not periods:
        return jsonify({'error': '请至少提交一条时间段'}), 400

    # ===== 先整体校验所有条目（全部通过才应用，避免部分生效） =====
    parsed = []
    for p in periods:
        if not isinstance(p, dict):
            return jsonify({'error': '时间段条目格式错误'}), 400
        ins = (p.get('insurance_type') or '').strip()
        if ins not in INSURANCE_TYPES:
            return jsonify({'error': f'险种必须为养老/医疗/工伤/失业保险之一: {ins or "(空)"}'}), 400
        s = (p.get('start') or '').strip()
        e = (p.get('end') or '').strip()
        if not s and not e:
            # 双空 → 清除覆盖，恢复识别值
            parsed.append((ins, None))
        elif s and e:
            s2, e2 = _valid_ym(s), _valid_ym(e)
            if not s2 or not e2:
                return jsonify({'error': f'{ins}: 起止年月格式应为 YYYY-MM，如 2023-01'}), 400
            if s2 > e2:
                return jsonify({'error': f'{ins}: 起始年月不能晚于截止年月'}), 400
            parsed.append((ins, (s2, e2)))
        else:
            return jsonify({'error': f'{ins}: 起止年月需同时填写（修改）或同时留空（恢复识别值）'}), 400

    with tasks_lock:
        result = tasks[task_id]['result']
        overrides = result.setdefault('_period_overrides', {})
        key = f'{name}|{idcard}'
        ins_map = overrides.setdefault(key, {})
        success_results = result.get('_success_results', [])
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        operator = session.get('username', '')
        log_entries = []

        for ins, val in parsed:
            # 旧值：优先取覆盖层，其次OCR识别值
            old_ov = ins_map.get(ins)
            old_ocr = None
            for r in success_results:
                if r.get('name') == name and r.get('insurance_type') == ins:
                    if not idcard or not r.get('idcard') or r['idcard'] == idcard:
                        if r.get('period'):
                            old_ocr = r['period']
                        break
            old_disp = old_ov or old_ocr

            if val is None:
                # 清除覆盖 → 恢复OCR识别值
                if ins in ins_map:
                    del ins_map[ins]
                    log_entries.append({
                        'time': now_str,
                        'action': '恢复识别结果',
                        'name': name,
                        'idcard': idcard,
                        'insurance_type': ins,
                        'old': _fmt_period(old_disp) or '无',
                        'new': _fmt_period(old_ocr) or '无',
                        'operator': operator,
                    })
                # 无覆盖时清空 = 无变化，不记录
            else:
                if old_disp != val:
                    ins_map[ins] = val
                    log_entries.append({
                        'time': now_str,
                        'action': '新增时间段' if not old_disp else '修改时间段',
                        'name': name,
                        'idcard': idcard,
                        'insurance_type': ins,
                        'old': _fmt_period(old_disp) or '无',
                        'new': _fmt_period(val),
                        'operator': operator,
                    })
                else:
                    # 值未变化，仅确保覆盖层存在
                    ins_map[ins] = val

        if not ins_map:
            overrides.pop(key, None)

        if log_entries:
            result.setdefault('_manual_log', []).extend(log_entries)
            for le in log_entries:
                logger.info(f'[task:{task_id}] {le["action"]}: {name}/{le["insurance_type"]} '
                            f'{le["old"]} → {le["new"]} (操作人: {operator})')

    # 统一重建（统计/Excel同步更新覆盖后的时间段）
    res = _rebuild_result(task_id)
    if res is None:
        return jsonify({'error': '重建结果失败'}), 500
    if 'yearly_ledger_files' in res:
        res['yearly_ledger_files'] = [f['filename'] for f in res['yearly_ledger_files']]
    return jsonify(res)


# ============ v1.1.45: 异常图片预览 ============
@insurance_bp.route('/api/image_preview/<task_id>')
@login_required
def api_image_preview(task_id):
    """返回指定文件的原图（供前端双击预览异常图片）

    查询参数 filename: image_details 中的文件名；
    依次在 失败/排除/有效 三桶记录中按文件名定位 _source_path 并返回文件内容。
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成'}), 400

    filename = (request.args.get('filename') or '').strip()
    if not filename:
        return jsonify({'error': '缺少文件名参数'}), 400

    inner = task.get('result') or {}
    for bucket_key in ('_failed_results', '_excluded_results', '_success_results'):
        for r in inner.get(bucket_key, []):
            if r.get('filename') != filename:
                continue
            src_path = r.get('_source_path', '')
            if src_path and os.path.exists(src_path):
                try:
                    return send_file(src_path)
                except Exception as e:
                    logger.error(f'[task:{task_id}] 图片预览失败: {filename}: {e}')
                    return jsonify({'error': f'图片读取失败: {e}'}), 500

    return jsonify({'error': '未找到该图片的源文件（可能已被重新上传覆盖）'}), 404


# ============ v1.1.45: 异常图片手动处理（命名/编辑信息/归入正常） ============
@insurance_bp.route('/api/update_excluded_image/<task_id>', methods=['POST'])
@login_required
def api_update_excluded_image(task_id):
    """异常图片手动处理：手动命名 + 编辑/补充异常信息，保存后归入正常列表

    入参 JSON: {filename, name, idcard?, insurance_type, start?, end?, new_name?, remark?}
    - filename: 定位异常记录（识别失败/缴费单位不一致/险种缺失/时间段缺失）
    - name: 姓名（必填）
    - idcard: 身份证号（选填，填写则做格式校验）
    - insurance_type: 四险之一（必填，决定归入哪个险种文件夹）
    - start/end: 起止年月 YYYY-MM（记录已有时间段时可留空沿用；均空且记录无时间段时报错）
    - new_name: 手动命名的新文件名主干（选填，自动保留原扩展名，替代"序号-姓名"规则命名）
    - remark: 异常信息备注（选填，编辑现有内容或新增补充说明）

    保存后记录并入有效列表（与自动识别同等效力参与统计），图片从"异常图片"
    文件夹移入对应险种文件夹；统计/Excel/文件整理全链路即时重建。
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '任务尚未完成'}), 400

    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    name = (data.get('name') or '').strip()
    idcard = (data.get('idcard') or '').strip()
    insurance_type = (data.get('insurance_type') or '').strip()
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    new_name = (data.get('new_name') or '').strip()
    remark = (data.get('remark') or '').strip()

    # ===== 参数校验 =====
    if not filename:
        return jsonify({'error': '缺少文件名参数'}), 400
    if not name:
        return jsonify({'error': '请输入姓名'}), 400
    if insurance_type not in INSURANCE_TYPES:
        return jsonify({'error': '险种必须为养老/医疗/工伤/失业保险之一'}), 400
    if idcard and not _validate_idcard(idcard):
        return jsonify({'error': '身份证号格式不正确，请核对（15位或18位）'}), 400
    # 手动命名：清洗 Windows 非法字符与首尾空格/点
    if new_name:
        for ch in '\\/:*?"<>|':
            new_name = new_name.replace(ch, '')
        new_name = new_name.strip().strip('.').strip()
        if not new_name:
            return jsonify({'error': '手动命名不能为空（仅含非法字符）'}), 400
        if len(new_name) > 80:
            return jsonify({'error': '手动命名过长（最多80个字符）'}), 400

    # 时间段：填写则校验并覆盖；均空则沿用记录现有值；记录也没有则必须填写
    period = None
    if start or end:
        start_ym = _valid_ym(start)
        end_ym = _valid_ym(end)
        if not start_ym or not end_ym:
            return jsonify({'error': '起始/截止年月格式应为 YYYY-MM，如 2023-01'}), 400
        if start_ym > end_ym:
            return jsonify({'error': '起始年月不能晚于截止年月'}), 400
        period = (start_ym, end_ym)

    with tasks_lock:
        result = tasks[task_id]['result']

        # ===== 三桶中按文件名定位记录 =====
        src_rec = None
        src_bucket_key = None
        for bucket_key in ('_failed_results', '_excluded_results', '_success_results'):
            for r in result.get(bucket_key, []):
                if r.get('filename') == filename:
                    src_rec = r
                    src_bucket_key = bucket_key
                    break
            if src_rec is not None:
                break
        if src_rec is None:
            return jsonify({'error': '未找到该图片记录（可能已被处理或重新上传覆盖）'}), 404

        # 判断是否异常记录（正常记录无需处理）
        is_abnormal = (src_rec.get('error') or src_rec.get('_excluded')
                       or (not src_rec.get('name') and not src_rec.get('idcard'))
                       or not src_rec.get('period'))
        if not is_abnormal and src_bucket_key == '_success_results':
            return jsonify({'error': '该图片已是正常状态，无需处理'}), 400

        if period is None:
            period = src_rec.get('period')
            if not period:
                return jsonify({'error': '该图片无识别时间段，请填写起止年月'}), 400

        # ===== 搬桶：失败/排除记录 → 有效列表（与自动识别同等效力） =====
        if src_bucket_key != '_success_results':
            result[src_bucket_key].remove(src_rec)
            result['_success_results'].append(src_rec)
            # 原属"缴费单位不一致"被排除的，同步从不一致文件列表移除
            result['_company_mismatch_files'] = [
                mf for mf in result.get('_company_mismatch_files', [])
                if mf.get('filename') != filename
            ]

        old_reason = '异常记录'
        if src_rec.get('_excluded'):
            old_reason = '缴费单位不一致（已排除）'
        elif src_rec.get('error'):
            old_reason = src_rec.get('error')
        elif not src_rec.get('period'):
            old_reason = '时间段缺失'
        src_rec.update({
            'name': name,
            'insurance_type': insurance_type,
            'period': period,
            'error': None,
            '_manual': True,
            '_resolved': True,
        })
        if idcard:
            src_rec['idcard'] = idcard
        src_rec.pop('_excluded', None)
        if new_name:
            src_rec['_manual_name'] = new_name
        if remark:
            src_rec['_remark'] = remark

        # 操作记录（可追溯）
        result.setdefault('_manual_log', []).append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '异常图片处理',
            'name': name,
            'idcard': idcard,
            'insurance_type': insurance_type,
            'old': old_reason,
            'new': f'{period[0]} ~ {period[1]}' + (f'，命名为 {new_name}' if new_name else ''),
            'operator': session.get('username', ''),
        })

    logger.info(f'[task:{task_id}] 异常图片处理: {filename} → {name}/{insurance_type} '
                f'{period[0]}~{period[1]}'
                + (f' 命名={new_name}' if new_name else '')
                + (f' 备注={remark}' if remark else '')
                + f' (操作人: {session.get("username", "")})')

    # 统一重建（统计/Excel/文件整理全链路同步：图片移入险种文件夹、移出异常文件夹）
    res = _rebuild_result(task_id)
    if res is None:
        return jsonify({'error': '重建结果失败'}), 500
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

    # 获取当前内部状态（v1.1.43: 三桶结构 有效/排除/失败）
    old_result = task.get('result', {})
    success_results = list(old_result.get('_success_results', []))
    excluded_results = list(old_result.get('_excluded_results', []))
    failed_results = list(old_result.get('_failed_results', []))
    task_dir = old_result.get('_task_dir', os.path.join(UPLOAD_DIR, task_id))
    company_name = old_result.get('_company_name', '') or old_result.get('company_name', '')
    company_mismatch_files = list(old_result.get('_company_mismatch_files',
                                                 old_result.get('company_mismatch_files', [])))

    # 获取花名册（表单优先，缺省回退原任务内部花名册）
    roster_json = request.form.get('roster', '[]')
    try:
        roster = json.loads(roster_json)
    except (json.JSONDecodeError, TypeError):
        roster = []
    if not roster:
        roster = old_result.get('_roster', [])

    # 保存并OCR新文件（记录上传原名，用于移除被覆盖的旧失败记录）
    new_paths = []
    retried_upload_names = set()
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
        retried_upload_names.add(f.filename)

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
                    '_source_path': fp,
                    '_source_origin': os.path.basename(fp),
                })
        else:
            img_basename = os.path.basename(fp)
            all_items.append((img_basename, fp, img_basename))

    for display_name, fp, source_origin in all_items:
        try:
            parsed = parse_ocr_result_from_image(fp)
            parsed['filename'] = display_name
            parsed['_source_path'] = fp
            parsed['_source_origin'] = source_origin
            new_results.append(parsed)
        except Exception as e:
            new_failed.append({
                'filename': display_name,
                'error': str(e),
                '_source_path': fp,
                '_source_origin': source_origin,
            })

    # 分类新结果
    retry_success = []
    for r in new_results:
        if r.get('error') or (not r.get('name') and not r.get('idcard')):
            new_failed.append(r)
        else:
            retry_success.append(r)

    # ===== 缴费单位过滤（与 process_task 一致：对四险做单位一致性校验） =====
    retry_valid = []
    for r in retry_success:
        cn = r.get('company_name', '').strip()
        need_check = (r.get('insurance_type') in INSURANCE_TYPES)
        if company_name and need_check and cn and cn != company_name:
            # 四险缴费单位不一致 → 排除
            r['_excluded'] = True
            excluded_results.append(r)
            company_mismatch_files.append({
                'filename': r.get('filename', ''),
                'ocr_company': cn,
                'expected_company': company_name,
            })
        else:
            retry_valid.append(r)

    # 移除被本次重传覆盖的旧失败记录（按上传文件名匹配）
    if retried_upload_names:
        failed_results = [r for r in failed_results
                          if r.get('filename', '') not in retried_upload_names]

    # 合并：原有有效 + 新有效；原有失败(未被覆盖) + 新失败
    success_results.extend(retry_valid)
    failed_results.extend(new_failed)

    logger.info(f'[task:{task_id}] 补充识别 — 新有效: {len(retry_valid)}, '
                f'排除: {len(retry_success) - len(retry_valid)}, 仍失败: {len(new_failed)}, '
                f'合并后总有效: {len(success_results)}')

    all_files = list(old_result.get('_all_files', old_result.get('all_files', []))) + \
                [os.path.basename(fp) for fp in new_paths]

    # ===== v1.1.43: 合并后写入内部状态，统一重建（统计/Excel/文件整理） =====
    with tasks_lock:
        tasks[task_id]['result'] = {
            '_success_results': success_results,
            '_excluded_results': excluded_results,
            '_failed_results': failed_results,
            '_all_files': all_files,
            '_task_dir': task_dir,
            '_year_range': old_result.get('_year_range'),
            '_roster': roster,
            '_roster_company': old_result.get('_roster_company', old_result.get('roster_company', '')),
            '_roster_source_path': old_result.get('_roster_source_path', ''),
            '_company_name': company_name,
            '_ocr_companies': old_result.get('_ocr_companies', old_result.get('ocr_companies', {})),
            '_company_mismatch_files': company_mismatch_files,
            '_period_overrides': old_result.get('_period_overrides', {}),
            '_manual_log': old_result.get('_manual_log', []),
        }

    res = _rebuild_result(task_id)
    if res is None:
        return jsonify({'error': '重建结果失败'}), 500
    logger.info(f'[task:{task_id}] 补充识别完成')

    # 过滤内部字段返回
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
