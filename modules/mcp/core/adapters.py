# -*- coding: utf-8 -*-
"""MCP 业务能力适配器

把三类业务能力包装为「登记任务 → 后台线程执行 → 状态可查 → 结果可取」的统一形态。
所有实现均直接复用各模块 core 的既有函数，不另起炉灶、不复制业务逻辑：

- 社保智能核算: modules.insurance.blueprint.process_task（复用其原生任务表）
- PDF/Word 转换: modules.pdf2word.core.converter.batch_convert / to_pdf.batch_to_pdf
- 劳动合同整理: modules.contract.core.file_renamer.plan_renames + execute_renames
"""
import os
import threading
from datetime import datetime

from core.paths import data_dir

from . import tasks

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

    thread = threading.Thread(
        target=ins_bp.process_task,
        args=(task_id, file_paths, roster or [], '', roster_path or '',
              year_range, tax_mode, province),
        daemon=True)
    thread.start()

    tasks.set_native(task_id, task_id)
    tasks.update(task_id, status='processing', total=len(file_paths),
                 message='社保核算进行中...')
    return thread


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
            tasks.finish(task_id, summary, '转换完成')
        except Exception as e:
            tasks.fail(task_id, e, '转换失败: %s' % e)

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
            tasks.finish(task_id, summary, '整理完成')
        except Exception as e:
            tasks.fail(task_id, e, '整理失败: %s' % e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
