# -*- coding: utf-8 -*-
"""MCP 业务能力适配器

把三类业务能力包装为「登记任务 → 后台线程执行 → 状态可查 → 结果可取」的统一形态。
所有实现均直接复用各模块 core 的既有函数，不另起炉灶、不复制业务逻辑：

- 社保智能核算: modules.insurance.blueprint.process_task（复用其原生任务表）
- PDF/Word 转换: modules.pdf2word.core.converter.batch_convert / to_pdf.batch_to_pdf
- 劳动合同整理: modules.contract.core.file_renamer.plan_renames + execute_renames

**交付模式（v2.2.1 瘦返回）**：任务完成时只把「精简摘要 + 文件卡片」存进任务表，
完整结果一律经 core/artifacts.py 落盘（含社保的 person_stats / image_details，
体量大且含身份证号，禁止内联回传）。
"""
import os
import threading
from datetime import datetime

from core.paths import data_dir

from . import artifacts, tasks

# 结果明细最多返回的条数（避免一次性回传过多内容给 AI）
MAX_DETAIL = 50


def output_dir_for(capability, task_id):
    """MCP 任务的统一输出目录：数据目录/outputs/mcp/<能力>/<任务id>"""
    path = os.path.join(data_dir(), 'outputs', 'mcp', capability, task_id)
    os.makedirs(path, exist_ok=True)
    return path


def _progress_cb(task_id, prefix):
    """兼容两种回调签名：fn(current,total) 与 fn(current,total,name,result)"""
    def cb(*args):
        if not args:
            return
        cur = args[0] or 0
        total = args[1] if len(args) > 1 else 0
        tasks.update(task_id, status='processing', current=cur, total=total,
                     message='%s (%s/%s)' % (prefix, cur, total or '?'))
    return cb


def _summarize(items, name_key='name', ok_key='ok'):
    """把批次结果压缩为 AI 易读的摘要"""
    ok_list = [i for i in items if i.get(ok_key)]
    bad_list = [i for i in items if not i.get(ok_key)]
    detail = []
    for i in items[:MAX_DETAIL]:
        detail.append({
            'name': i.get(name_key),
            'out_name': i.get('out_name') or i.get('docx_name'),
            'ok': bool(i.get(ok_key)),
            'error': i.get('error'),
        })
    return {
        'total': len(items),
        'success': len(ok_list),
        'failed': len(bad_list),
        'files': detail,
        'truncated': len(items) > MAX_DETAIL,
    }


def _finalize(task_id, capability, out_dir, summary, message, prefix):
    """任务完成：完整结果落盘 → 生成卡片 → 任务表只留精简摘要与卡片（瘦返回）"""
    card = artifacts.write_json(capability, task_id, prefix, summary)
    out_cards, out_total, out_trunc = artifacts.collect_output_cards(out_dir)
    payload = {
        'summary': artifacts.slim(summary),
        'artifacts': [card] + out_cards,
        'output_dir': out_dir,
        'output_file_count': out_total,
        'output_list_truncated': out_trunc,
    }
    tasks.finish(task_id, payload, message)
    return payload


def _fail_with_report(task_id, capability, error, message):
    """失败降级：错误报告落盘留痕，任务表只留精简错误与卡片"""
    card = artifacts.write_error(capability, task_id, error,
                                 {'task_id': task_id, 'capability': capability})
    tasks.fail(task_id, error, message)
    tasks.update(task_id, artifacts=[card])
    return card


# ==================== 社保智能核算 ====================

def start_insurance(task_id, file_paths, province, tax_mode='退税',
                    roster_path=None, year_range=None):
    """启动社保核算：直接复用 insurance 模块的 process_task（含 OCR/解析/统计/出表）

    复用要点：process_task 依赖宿模块的任务表 tasks[task_id] 读取取消/暂停标记
    并回写进度，故必须先按其结构注册任务，再起线程执行。
    """
    from modules.insurance import blueprint as ins_bp
    from modules.insurance.core import template_engine
    from modules.insurance.core.roster_parser import parse_roster_from_table

    available = template_engine.get_provinces()
    codes = {p.get('province_code') for p in available}
    if province not in codes:
        names = '、'.join(p.get('province_name', '') for p in available)
        raise ValueError('省份不支持或未提供（当前可用：%s）' % names)
    if tax_mode not in ('退税', '抵税'):
        tax_mode = '退税'

    roster = parse_roster_from_table(roster_path) if roster_path else None

    with ins_bp.tasks_lock:
        ins_bp.tasks[task_id] = {
            'status': 'pending',
            'current': 0,
            'total': len(file_paths),
            'message': '等待处理...',
            'files': [os.path.basename(p) for p in file_paths],
            'result': None,
            'created_at': datetime.now().isoformat(),
            'paused': False,
            'cancelled': False,
        }

    args = (task_id, file_paths, roster or [], '', roster_path or '',
            year_range, tax_mode, province)
    # 先登记映射与初始状态再起线程：否则极快完成的任务会被随后的
    # update(status='processing') 覆盖回进行中（竞态）
    tasks.set_native(task_id, task_id)
    tasks.update(task_id, status='processing', total=len(file_paths),
                 message='社保核算进行中...')

    thread = threading.Thread(target=_insurance_worker, args=args, daemon=True)
    thread.start()
    return thread


def _insurance_worker(*args):
    """社保核算线程包装：先跑宿模块 process_task，返回后再物化产物并落盘

    包一层的原因：process_task 只写宿模块原生任务表，不会回调 MCP；
    包装后可在其返回或抛错时统一落盘完整结果 / 错误报告。
    """
    task_id = args[0]
    from modules.insurance import blueprint as ins_bp
    try:
        ins_bp.process_task(*args)
    except Exception as e:
        _fail_with_report(task_id, 'insurance', e, '社保核算失败: %s' % e)
        return
    materialize_insurance(task_id)


def materialize_insurance(task_id):
    """社保结果落盘 + 卡片化，任务表只留精简摘要

    result 含 person_stats / image_details（逐人逐图且带身份证号），
    体量大且敏感，禁止内联回传，只回传统计口径与文件卡片。
    """
    native = insurance_native(task_id) or {}
    result = native.get('result')
    status = native.get('status')
    if result is None or status == 'error':
        err = native.get('error') or native.get('message') or '社保核算未完成'
        _fail_with_report(task_id, 'insurance', err, '社保核算失败: %s' % err)
        return None

    out_dir = ''
    if isinstance(result, dict) and result.get('excel_path'):
        out_dir = os.path.dirname(os.path.abspath(result['excel_path']))

    card = artifacts.write_json('insurance', task_id, 'insurance', result,
                                label='完整核算结果（JSON，含逐人参保明细）')
    out_cards, out_total, out_trunc = artifacts.collect_output_cards(out_dir)

    if isinstance(result, dict):
        summary = {
            'person_count': result.get('person_count'),
            'ocr_count': result.get('ocr_count'),
            'success_count': result.get('success_count'),
            'excluded_count': result.get('excluded_count'),
            'failed_count': result.get('failed_count'),
            'tax_mode': result.get('tax_mode'),
            'year_cols': result.get('year_cols'),
            'excel_filename': result.get('excel_filename'),
            'yearly_ledger_count': len(result.get('yearly_ledger_files') or []),
            'company_name': result.get('company_name'),
            'person_stats_count': len(result.get('person_stats') or []),
            'image_details_count': len(result.get('image_details') or []),
        }
    else:
        summary = {'raw': str(result)}

    payload = {
        'summary': summary,
        'artifacts': [card] + out_cards,
        'output_dir': out_dir,
        'output_file_count': out_total,
        'output_list_truncated': out_trunc,
    }
    tasks.finish(task_id, payload, '核算完成')
    return payload


def insurance_native(task_id):
    """实时回读 insurance 原生任务状态（进度/消息/结果）"""
    try:
        from modules.insurance import blueprint as ins_bp
        with ins_bp.tasks_lock:
            native = ins_bp.tasks.get(task_id)
            return dict(native) if native else None
    except Exception:
        return None


# ==================== PDF / Word 双向转换 ====================

def start_pdf2word(task_id, file_paths, direction='pdf2word',
                   output_mode='individual'):
    """启动文档转换：pdf2word=PDF转Word；topdf=其他格式转PDF"""
    out_dir = output_dir_for('pdf2word', task_id)
    cb = _progress_cb(task_id, '正在转换')

    def _run():
        try:
            tasks.update(task_id, status='processing', total=len(file_paths),
                         message='开始转换...')
            if direction == 'pdf2word':
                from modules.pdf2word.core import converter
                items = converter.batch_convert(file_paths, out_dir,
                                                progress_callback=cb)
                results = [{
                    'name': os.path.basename(r.get('pdf_name') or ''),
                    'out_name': r.get('docx_name'),
                    'ok': bool(r.get('ok')),
                    'error': r.get('error'),
                } for r in (items or [])]
                summary = _summarize(results, name_key='name')
            else:
                from modules.pdf2word.core import to_pdf
                items, skipped = to_pdf.batch_to_pdf(
                    file_paths, out_dir, output_mode=output_mode,
                    progress_callback=cb)
                results = [{
                    'name': r.get('name'),
                    'out_name': r.get('out_name'),
                    'ok': True,
                    'error': None,
                } for r in (items or [])]
                results += [{
                    'name': s.get('name'),
                    'out_name': None,
                    'ok': False,
                    'error': s.get('reason'),
                } for s in (skipped or [])]
                summary = _summarize(results, name_key='name')
            summary['output_dir'] = out_dir
            summary['direction'] = direction
            _finalize(task_id, 'pdf2word', out_dir, summary, '转换完成', direction)
        except Exception as e:
            _fail_with_report(task_id, 'pdf2word', e, '转换失败: %s' % e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ==================== 劳动合同整理 ====================

def start_contract(task_id, file_paths, roster_path=None):
    """启动合同整理：按花名册智能匹配并重命名归档

    无人值守策略（替代前端人工确认环节）：
    - plan['auto'] 自动匹配项 → 直接重命名
    - plan['unmatched'] 未匹配 + plan['duplicates'] 重名待确认 → 移入「待处理」
    """
    out_dir = output_dir_for('contract', task_id)
    cb = _progress_cb(task_id, '正在整理')

    def _run():
        try:
            from modules.contract.core.roster_parser import parse_roster_from_table
            from modules.contract.core.file_renamer import (
                plan_renames, validate_renames, execute_renames)

            roster = parse_roster_from_table(roster_path) if roster_path else []
            if not roster:
                raise ValueError('未提供花名册或花名册解析为空（roster_path）')

            tasks.update(task_id, status='processing', total=len(file_paths),
                         message='正在生成重命名计划...')
            plan = plan_renames(file_paths, roster)

            renames = [{'original': a.get('original'),
                        'new_name': a.get('new_name'),
                        'seq': a.get('seq')}
                       for a in plan.get('auto', [])]
            pending = [u.get('original') for u in plan.get('unmatched', [])
                       if u.get('original')]
            for d in plan.get('duplicates', []):
                orig = d.get('original') or d.get('basename')
                if orig:
                    pending.append(orig)

            source_paths = {os.path.basename(p): p for p in file_paths}
            errors = validate_renames(source_paths, renames)
            if errors:
                raise ValueError('；'.join(errors[:5]))

            result = execute_renames(source_paths, plan, out_dir, renames,
                                     pending, progress_callback=cb,
                                     task_id=task_id)
            summary = {
                'output_dir': out_dir,
                'total': plan.get('total', len(file_paths)),
                'renamed': len(renames),
                'pending': len(pending),
                'unmatched': len(plan.get('unmatched', [])),
                'duplicates': len(plan.get('duplicates', [])),
                'roster_missing': len(plan.get('roster_missing', [])),
                'detail': result if isinstance(result, dict) else {'raw': str(result)},
            }
            _finalize(task_id, 'contract', out_dir, summary, '整理完成', 'contract')
        except Exception as e:
            _fail_with_report(task_id, 'contract', e, '整理失败: %s' % e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ==================== 合同整理两阶段（v2.3.2 预览/确认） ====================

def preview_contract(task_id, file_paths, roster_path=None):
    """同步生成重命名计划（不执行），任务置 waiting_confirm 并暂存计划数据

    与平台「重命名计划预览」对应：把需要人工选择的项（重名待确认，含候选
    归属人）返回给调用方，由用户选择后再 confirm_contract 执行。
    """
    from modules.contract.core.roster_parser import parse_roster_from_table
    from modules.contract.core.file_renamer import plan_renames

    roster = parse_roster_from_table(roster_path) if roster_path else []
    if not roster:
        raise ValueError('未提供花名册或花名册解析为空（roster_path）')

    plan = plan_renames(file_paths, roster)
    dup_count = len(plan.get('duplicates', []))
    tasks.update(
        task_id, status='waiting_confirm',
        total=plan.get('total', len(file_paths)),
        message='计划已生成，等待确认（重名待确认 %d 项）' % dup_count,
        pending_contract={'file_paths': list(file_paths),
                          'roster_path': roster_path,
                          'plan': plan})
    return plan


def _choice_maps(choices):
    """解析确认选择：new_name 覆盖表 + 重名归属 seq 表（键均为原文件名）"""
    override_name = {}
    assign_seq = {}
    for c in choices or []:
        if not isinstance(c, dict):
            continue
        orig = (c.get('original') or '').strip()
        if not orig:
            continue
        if c.get('new_name'):
            override_name[orig] = str(c['new_name']).strip()
        if c.get('seq') is not None:
            try:
                assign_seq[orig] = int(c['seq'])
            except (TypeError, ValueError):
                pass
    return override_name, assign_seq


def build_confirmed_renames(plan, choices):
    """按计划 + 确认选择合成最终重命名名单与待处理名单

    - auto 项全部重命名（new_name 可被 choices 覆盖）
    - 重名项：choices 指定 seq 且命中候选 → 生成新名（可被 new_name 覆盖）；
      未选择归属的重名项 → 待处理
    - 未匹配项 → 待处理

    返回 (renames, pending)。
    """
    override_name, assign_seq = _choice_maps(choices)

    renames = []
    for a in plan.get('auto', []):
        orig = a.get('original')
        renames.append({'original': orig,
                        'new_name': override_name.get(orig) or a.get('new_name'),
                        'seq': a.get('seq')})

    pending = [u.get('original') for u in plan.get('unmatched', [])
               if u.get('original')]
    for d in plan.get('duplicates', []):
        orig = d.get('original') or d.get('basename')
        if not orig:
            continue
        seq = assign_seq.get(orig)
        cand = None
        if seq is not None:
            for c in d.get('candidates', []):
                if c.get('seq') == seq:
                    cand = c
                    break
        if cand is None:
            pending.append(orig)
            continue
        new_name = override_name.get(orig)
        if not new_name:
            ext = os.path.splitext(orig)[1]
            tail = (cand.get('idcard_tail') or '').strip()
            base = '%02d-%s' % (cand.get('seq') or 0, cand.get('name') or '')
            if tail:
                base = '%s-%s' % (base, tail)
            new_name = base + ext
        renames.append({'original': orig, 'new_name': new_name,
                        'seq': cand.get('seq')})

    return renames, pending


def confirm_contract(task_id, choices=None):
    """按确认选择执行合同重命名（后台线程）

    前置：任务处于 waiting_confirm（preview_contract 已暂存计划）。
    未在 choices 中选择归属的重名项与未匹配项移入「待处理」。
    """
    task = tasks.get(task_id) or {}
    pending_data = task.get('pending_contract') or {}
    plan = pending_data.get('plan') or {}
    file_paths = pending_data.get('file_paths') or []
    if not plan or not file_paths:
        raise ValueError('任务缺少待确认计划数据，请重新 preview 后再确认')

    out_dir = output_dir_for('contract', task_id)
    cb = _progress_cb(task_id, '正在整理')
    renames, pending = build_confirmed_renames(plan, choices)

    def _run():
        try:
            from modules.contract.core.file_renamer import (
                validate_renames, execute_renames)

            source_paths = {os.path.basename(p): p for p in file_paths}
            errors = validate_renames(source_paths, renames)
            if errors:
                raise ValueError('；'.join(errors[:5]))

            tasks.update(task_id, status='processing',
                         total=len(file_paths),
                         message='正在执行重命名...')
            result = execute_renames(source_paths, plan, out_dir, renames,
                                     pending, progress_callback=cb,
                                     task_id=task_id)
            summary = {
                'output_dir': out_dir,
                'total': plan.get('total', len(file_paths)),
                'renamed': len(renames),
                'pending': len(pending),
                'unmatched': len(plan.get('unmatched', [])),
                'duplicates_resolved': len(renames) - len(plan.get('auto', [])),
                'duplicates': len(plan.get('duplicates', [])),
                'roster_missing': len(plan.get('roster_missing', [])),
                'confirmed': True,
                'detail': result if isinstance(result, dict) else {'raw': str(result)},
            }
            tasks.update(task_id, pending_contract=None)
            _finalize(task_id, 'contract', out_dir, summary, '整理完成（已确认）',
                      'contract')
        except Exception as e:
            _fail_with_report(task_id, 'contract', e, '整理失败: %s' % e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
