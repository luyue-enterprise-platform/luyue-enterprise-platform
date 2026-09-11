# -*- coding: utf-8 -*-
"""MCP 工具定义

每个工具 = 名称 + 说明 + JSON Schema 入参 + 处理器。
处理器统一返回 (text, is_error)：text 为回传给 AI 的可读文本（JSON），
is_error=True 时按 MCP 规范标记工具执行失败。

**瘦返回交付模式（v2.2.1）**
响应只回「精简摘要 + 执行状态 + 结果规模 + 文件卡片」：
- 批量明细（逐文件结果、逐人参保记录、逐图识别详情）一律不内联，
  由 core/artifacts.py 落盘为 JSON，响应只给卡片（name/path/uri/size/lines/mime）
- 未完成任务 / 失败任务：给出明确错误、当前状态、已运行时长与后续建议，
  失败同时落盘错误报告便于追溯

**集中式缺参校验（v2.3.0 设计）**
提交类工具入参不全时，一次性返回全部阻塞项（blocking：字段/问题/修正方法/候选值）
与可选项默认值（optional_defaults），不逐项报错，AI 据此发起一轮集中式询问。

**Server 端等待（v2.3.0 设计）**
提交类工具支持 wait_seconds 参数（默认 0 立即返回 task_id）：
大批量长耗时任务可让 Server 在内部轮询等待至完成，一次性返回全部结果，
避免 AI 反复调用"好了吗"。超时未完成则返回明确降级信息（任务仍在后台运行）。

批量能力均为长耗时操作，统一返回 task_id，由 get_task_status / get_task_result
完成后续轮询与取结果。
"""
import json
import logging
import os
import time
from datetime import datetime

from . import adapters, artifacts, tasks

logger = logging.getLogger('mcp.tools')

# 判定为"疑似卡死"的运行时长阈值（秒）：仅用于给出提示，不主动结束任务
STALE_AFTER_SEC = 30 * 60
# Server 端内部等待上限（秒）：大批量 OCR 任务十几分钟，给足 20 分钟
MAX_WAIT_SEC = 20 * 60
# Server 端内部轮询间隔（秒）
POLL_INTERVAL_SEC = 0.8

INSURANCE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf'}
PDF_EXTS = {'.pdf'}
CONTRACT_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf'}
TOPDF_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif', '.tiff', '.webp',
              '.doc', '.docx', '.xls', '.xlsx', '.txt', '.md', '.pdf'}


def _expand(paths, exts):
    """展开为文件列表：目录递归收集指定扩展名，文件校验存在性与类型"""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise ValueError('file_paths 必须为非空数组')
    out = []
    for p in paths:
        p = str(p).strip()
        if not p:
            continue
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in sorted(files):
                    if os.path.splitext(fn)[1].lower() in exts:
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            if os.path.splitext(p)[1].lower() not in exts:
                raise ValueError('不支持的文件类型: %s' % p)
            out.append(p)
        else:
            raise ValueError('路径不存在: %s' % p)
    if not out:
        raise ValueError('未找到可处理的%s文件' % ('' if not exts else '（%s）' % ','.join(sorted(exts))))
    return out


def _year_range(args):
    y1, m1 = args.get('year_start'), args.get('month_start')
    y2, m2 = args.get('year_end'), args.get('month_end')
    if None in (y1, m1, y2, m2):
        return None
    try:
        start = '%04d-%02d' % (int(y1), int(m1))
        end = '%04d-%02d' % (int(y2), int(m2))
    except (TypeError, ValueError):
        raise ValueError('统计时间段参数必须为整数（year_start/month_start/year_end/month_end）')
    if start > end:
        raise ValueError('统计时间段起始不得晚于截止')
    return (start, end)


def _ok(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2), False


def _err(msg):
    return json.dumps({'error': str(msg)}, ensure_ascii=False, indent=2), True


def _elapsed(task):
    """任务已运行秒数（用于超时/卡死判定提示）"""
    try:
        start = datetime.fromisoformat(task.get('created_at'))
        return max(0, int((datetime.now() - start).total_seconds()))
    except Exception:
        return None


def _elapsed_human(sec):
    if not sec and sec != 0:
        return '-'
    if sec < 60:
        return '%d 秒' % sec
    if sec < 3600:
        return '%d 分 %d 秒' % (sec // 60, sec % 60)
    return '%d 小时 %d 分' % (sec // 3600, (sec % 3600) // 60)


def _artifacts_of(task):
    result = task.get('result')
    if isinstance(result, dict):
        return result.get('artifacts') or []
    return task.get('artifacts') or []


# v2.3.8 返回安全化：完成态摘要允许内联的"小型标量结构"上限——
# 超过该规模的列表/字典视为明细数据，一律降为计数，不进返回内容。
_SUMMARY_INLINE_LIST_LIMIT = 8
_SUMMARY_INLINE_DICT_LIMIT = 12


def _scrub_summary(summary):
    """返回内容安全化（v2.3.8）：台账/表格类具体数据不得出现在任务返回中。

    规则：摘要只内联标量统计与小型枚举（如年份列、险种分布）；
    任何行级列表（逐人/逐图/逐文件明细）与嵌套结构一律降为 <key>_count
    计数。完整敏感数据只存在于落盘文件（artifacts 卡片引用其路径），
    绝不进入任务返回内容，防止敏感信息随任务结果外泄。
    """
    if not isinstance(summary, dict):
        return summary
    out = {}
    for k, v in summary.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            if (len(v) <= _SUMMARY_INLINE_LIST_LIMIT and
                    all(isinstance(x, (str, int, float, bool)) for x in v)):
                out[k] = v
            else:
                out['%s_count' % k] = len(v)
        elif isinstance(v, dict):
            if (len(v) <= _SUMMARY_INLINE_DICT_LIMIT and
                    all(isinstance(x, (str, int, float, bool))
                        for x in v.values())):
                out[k] = v
            else:
                out['%s_count' % k] = len(v)
        else:
            out['%s_count' % k] = 1
    return out


def _thin_payload(task, hint=None):
    """统一瘦返回体（v2.3.8 安全化）：状态 + 清洗后摘要 + 结果规模 + 文件卡片

    返回内容只包含：任务是否成功完成（status/message）、统计计数、
    结果文件的存放路径（output_dir + artifacts 卡片）。
    台账/表格等敏感文件的具体数据一律不内联——summary 经 _scrub_summary
    强制清洗（行级明细降为计数），完整数据仅在落盘文件中。
    """
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    cards = _artifacts_of(task)
    return {
        'task_id': task.get('task_id'),
        'capability': task.get('capability'),
        'status': task.get('status'),
        'message': task.get('message'),
        'summary': _scrub_summary(result.get('summary')),
        'result_size': artifacts.result_size(cards),
        'artifacts': cards,
        'output_dir': result.get('output_dir'),
        'elapsed_sec': _elapsed(task),
        'elapsed_human': _elapsed_human(_elapsed(task)),
        'hint': hint or ('任务已完成。敏感明细（台账/表格/逐人数据）仅保存在落盘文件中，'
                         '不随返回内容外泄；请通过 artifacts 文件卡片的存放路径查看、'
                         '下载或打开结果'),
    }


def _err_payload(msg, task, suggestion=None):
    """错误与降级响应：明确错误信息 + 当前状态 + 已运行时长 + 后续建议
    （已有落盘文件时附卡片，便于追溯）"""
    payload = {
        'error': str(msg),
        'task_id': task.get('task_id'),
        'capability': task.get('capability'),
        'status': task.get('status'),
        'current': task.get('current'),
        'total': task.get('total'),
        'progress': task.get('progress'),
        'elapsed_sec': _elapsed(task),
        'elapsed_human': _elapsed_human(_elapsed(task)),
    }
    cards = _artifacts_of(task)
    if cards:
        payload['artifacts'] = cards
    if suggestion:
        payload['suggestion'] = suggestion
    return json.dumps(payload, ensure_ascii=False, indent=2), True


def _sync_insurance(task):
    """社保任务：回读原生进度；原生完成后物化产物（落盘+卡片），只回精简视图"""
    if task.get('capability') != 'insurance' or not task.get('native_task_id'):
        return task
    native = adapters.insurance_native(task['native_task_id']) or {}
    if not native:
        return task
    task.update({
        'status': native.get('status', task.get('status')),
        'current': native.get('current', task.get('current')),
        'total': native.get('total', task.get('total')),
        'message': native.get('message', task.get('message')),
    })
    if native.get('result') is not None:
        task_id = task.get('task_id')
        stored = tasks.get(task_id) or {}
        if not (stored.get('result') or {}).get('artifacts'):
            # 幂等物化：worker 线程已做则跳过，线程异常时由此兜底
            adapters.materialize_insurance(task_id)
        task = tasks.get(task_id) or task
    return task


def _missing_payload(issues, optional_defaults):
    """集中式缺参响应：一次性列出全部阻塞项与可选项默认值，任务不提交。

    blocking 项 = 缺失后任务无法执行的必填项（含问题、修正方法、候选值）；
    optional_defaults = 可不填的项及其默认值。AI 据此向用户发起一轮集中式询问，
    而非逐项追问。
    """
    payload = {
        'error': '必填项缺失或非法，任务未提交。请一次性补齐下列全部 blocking 项后重新调用本工具',
        'blocking': issues,
        'optional_defaults': optional_defaults,
        'suggestion': '请参照每项的 how_to_fix 与 candidates，在一条指令中一次性补齐全部 '
                      'blocking 项；optional_defaults 所列项可不填，将按默认值执行。'
                      'candidates 候选值与默认值须呈现给用户选择确认，'
                      '不得由调用方代替用户决定',
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), True


def _province_candidates():
    """社保核算省份候选列表（用于缺参响应中的候选值提示）"""
    try:
        from modules.insurance.core import template_engine
        return [{'value': p.get('province_code'), 'label': p.get('province_name')}
                for p in template_engine.get_provinces()]
    except Exception:
        return None


def _check_wait(args):
    """校验并解析 wait_seconds（Server 端内部等待秒数）。

    返回 (issue, value)：issue 非 None 时为集中式缺参响应的阻塞项，
    value 为钳制后的等待秒数（默认 0 = 立即返回 task_id）。
    """
    raw = args.get('wait_seconds', 0)
    if raw in (None, ''):
        return None, 0
    try:
        sec = int(raw)
    except (TypeError, ValueError):
        return ({
            'field': 'wait_seconds',
            'issue': 'wait_seconds 必须为 0-%d 的整数（秒），当前为: %s' % (MAX_WAIT_SEC, raw),
            'how_to_fix': '不填或传 0 立即返回 task_id；大批量任务传 600-1200 由 '
                          'Server 内部等待并一次性返回全部结果',
        }, 0)
    if sec < 0 or sec > MAX_WAIT_SEC:
        return ({
            'field': 'wait_seconds',
            'issue': 'wait_seconds 须在 0-%d 范围内，当前为: %s' % (MAX_WAIT_SEC, sec),
            'how_to_fix': '不填或传 0 立即返回 task_id；大批量任务建议 600-1200',
        }, 0)
    return None, sec


def _wait_and_fetch(task_id, wait_seconds):
    """Server 端内部等待（v2.3.4 起带耗时埋点）：进入/退出各记一条日志，
    实际等待时长可与 access 日志、任务日志互相印证"""
    started = time.monotonic()
    logger.info('[mcp.wait] task=%s 进入 Server 端等待（上限 %ss）',
                task_id, wait_seconds)
    text, is_error = _wait_and_fetch_inner(task_id, wait_seconds)
    logger.info('[mcp.wait] task=%s 退出等待：实际 %.1fs is_error=%s',
                task_id, time.monotonic() - started, is_error)
    return text, is_error


def _wait_and_fetch_inner(task_id, wait_seconds):
    """Server 端内部等待：轮询至终态或期限，完成即一次性返回瘦结果。

    避免外部 AI 反复轮询"好了吗"；超时未完成返回明确降级信息
    （任务未失败、仍在后台运行、结果照常落盘）。
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        task = tasks.get(task_id)
        if not task:
            return _err('任务不存在: %s' % task_id)
        task = _sync_insurance(task)
        status = task.get('status')
        if status == 'error':
            return _err_payload(
                task.get('error') or '任务执行失败', task,
                suggestion='错误报告已落盘（见 artifacts）；请核对入参与文件后重新提交')
        if status == 'success':
            return _ok(_thin_payload(
                task, hint='任务已完成，Server 已内部等待并一次性返回全部结果（耗时 %s）；'
                           '完整明细已落盘，见 artifacts 文件卡片'
                           % _elapsed_human(_elapsed(task))))
        if status == 'waiting_confirm':
            # v2.3.2 合同两阶段 / v2.3.5 社保两阶段 / v2.3.8 转换两阶段：
            # 计划待确认不是"执行中"，直接返回引导而非空等
            if task.get('capability') == 'insurance':
                return _err_payload(
                    '任务待确认：核算预览已生成，尚未开始识别', task,
                    suggestion='调用 insurance_calculate 传 confirm_task_id=%s '
                               '确认执行（可在同一次调用中覆盖预览参数）' % task_id)
            if task.get('capability') == 'pdf2word':
                direction = (task.get('pending_pdf2word') or {}).get('direction')
                tool_name = 'convert_to_pdf' if direction == 'topdf' \
                    else 'convert_pdf_to_word'
                return _err_payload(
                    '任务待确认：转换预览已生成，尚未执行任何转换', task,
                    suggestion='调用 %s 传 confirm_task_id=%s 确认执行'
                               '（转PDF可在确认时以 output_mode 覆盖输出方式）'
                               % (tool_name, task_id))
            return _err_payload(
                '任务待确认：重命名计划已生成，尚未执行任何重命名', task,
                suggestion='调用 contract_organize 传 confirm_task_id=%s 与 choices '
                           '确认执行' % task_id)
        if time.monotonic() >= deadline:
            return _err_payload(
                'Server 已内部等待 %d 秒，任务仍在处理中'
                '（任务未失败，仍在后台运行，结果完成后照常落盘）' % wait_seconds, task,
                suggestion='可稍后调用 get_task_status 轮询进度，完成后调用 '
                           'get_task_result 一次性取精简摘要与文件卡片；'
                           '大批量任务建议提交时携带 wait_seconds=600-1200')
        time.sleep(POLL_INTERVAL_SEC)


def _immediate_payload(task_id, capability, file_count, message, effective=None):
    """wait_seconds=0 时的立即返回体：只给 task_id 与去向，不等待

    v2.3.2：effective_params 回显本次实际生效的参数（含各项默认值），
    让"平台上需要选择的项"在 MCP 响应中可见、可核对。
    """
    body = {
        'task_id': task_id,
        'file_count': file_count,
        'status': 'processing',
        'message': message,
        'result_dir': artifacts.artifact_dir(capability, task_id),
        'hint': '任务已提交；大批量长耗时任务可在提交时携带 wait_seconds（0-%d 秒）'
                '由 Server 内部等待并一次性返回全部结果；完成后调用 get_task_result '
                '取精简摘要与文件卡片，完整明细一律落盘' % MAX_WAIT_SEC,
    }
    if effective:
        body['effective_params'] = effective
    return _ok(body)


YEAR_KEYS = ('year_start', 'month_start', 'year_end', 'month_end')


def _issue_file_paths(args, exts):
    """集中校验 file_paths：返回 (issues, file_paths)"""
    issues = []
    file_paths = None
    try:
        file_paths = _expand(args.get('file_paths'), exts)
    except Exception as e:
        issues.append({
            'field': 'file_paths',
            'issue': str(e),
            'how_to_fix': '提供本机存在的文件或目录路径数组（支持 %s），目录将自动递归收集'
                          % '/'.join(sorted(exts)),
        })
    return issues, file_paths


def _validate_insurance(args):
    """社保核算集中校验：一次性收集全部问题与可选项默认值"""
    issues, file_paths = _issue_file_paths(args, INSURANCE_EXTS)

    province = (args.get('province') or '').strip()
    if not province:
        item = {
            'field': 'province',
            'issue': 'province 为必填项，当前缺失，任务无法执行',
            'how_to_fix': '从 candidates 中选择省份代码（value 字段），'
                          '或先调用 insurance_provinces 查询完整列表',
        }
        candidates = _province_candidates()
        if candidates:
            item['candidates'] = candidates
        issues.append(item)

    tax_mode = args.get('tax_mode', '退税')
    if tax_mode not in ('退税', '抵税'):
        issues.append({
            'field': 'tax_mode',
            'issue': "tax_mode 只能是 '退税' 或 '抵税'，当前为: %s" % tax_mode,
            'how_to_fix': "不填默认 '退税'；需要抵税时显式传 '抵税'",
        })

    year_range = None
    present = [k for k in YEAR_KEYS if args.get(k) not in (None, '')]
    if present and len(present) != len(YEAR_KEYS):
        missing = [k for k in YEAR_KEYS if k not in present]
        issues.append({
            'field': '/'.join(YEAR_KEYS),
            'issue': '统计时间段须四项同时提供，当前缺失: %s' % ', '.join(missing),
            'how_to_fix': '同时提供 %s（均为整数），或四项全部省略（按参保数据全区间统计）'
                          % ', '.join(YEAR_KEYS),
        })
    elif present:
        try:
            year_range = _year_range(args)
        except Exception as e:
            issues.append({
                'field': '/'.join(YEAR_KEYS),
                'issue': str(e),
                'how_to_fix': '年月均为整数，且起始不得晚于截止（如 2023-01 至 2025-12）',
            })

    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        issues.append(wait_issue)

    optional = [
        {'field': 'tax_mode', 'default': '退税', 'note': '税种模式，可选 退税/抵税'},
        {'field': 'roster_path', 'default': '不使用',
         'note': '花名册文件路径（用于人员比对），可不填'},
        {'field': '/'.join(YEAR_KEYS), 'default': '参保数据全区间',
         'note': '统计时间段，四项整体可选'},
        {'field': 'wait_seconds', 'default': 0,
         'note': 'Server 内部等待秒数（0-%d），大批量建议 600-1200，等待完成一次性返回结果'
                 % MAX_WAIT_SEC},
    ]
    return issues, optional, file_paths, province, tax_mode, year_range, wait_seconds


def _validate_pdf2word(args, exts, direction):
    """PDF 双向转换集中校验：一次性收集全部问题与可选项默认值"""
    issues, file_paths = _issue_file_paths(args, exts)
    output_mode = None
    optional = []
    if direction == 'topdf':
        output_mode = args.get('output_mode') or 'individual'
        if output_mode not in ('individual', 'merge'):
            issues.append({
                'field': 'output_mode',
                'issue': "output_mode 只能是 'individual' 或 'merge'，当前为: %s" % output_mode,
                'how_to_fix': "不填默认 'individual'（逐个输出）；需合并为单个 PDF 时传 'merge'",
            })
        optional.append({'field': 'output_mode', 'default': 'individual',
                         'note': '输出方式，可选 individual（逐个）/merge（合并单PDF）'})
    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        issues.append(wait_issue)
    optional.append({'field': 'wait_seconds', 'default': 0,
                     'note': 'Server 内部等待秒数（0-%d），大批量建议 600-1200，'
                             '等待完成一次性返回结果' % MAX_WAIT_SEC})
    return issues, optional, file_paths, output_mode, wait_seconds


def _validate_contract(args):
    """合同整理集中校验：一次性收集全部问题与可选项默认值"""
    issues, file_paths = _issue_file_paths(args, CONTRACT_EXTS)
    roster_path = args.get('roster_path')
    if not roster_path or not str(roster_path).strip():
        issues.append({
            'field': 'roster_path',
            'issue': 'roster_path 为必填项（合同按花名册匹配重命名），当前缺失，任务无法执行',
            'how_to_fix': '提供花名册文件路径（Excel/CSV，含姓名列，姓名须与影像内容一致）',
        })
    elif not os.path.isfile(str(roster_path).strip()):
        issues.append({
            'field': 'roster_path',
            'issue': '花名册文件不存在: %s' % roster_path,
            'how_to_fix': '核对本机路径是否正确；须为已存在的 Excel/CSV 文件',
        })
    else:
        roster_path = str(roster_path).strip()
    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        issues.append(wait_issue)
    optional = [
        {'field': 'wait_seconds', 'default': 0,
         'note': 'Server 内部等待秒数（0-%d），大批量建议 600-1200，等待完成一次性返回结果'
                 % MAX_WAIT_SEC},
    ]
    return issues, optional, file_paths, roster_path, wait_seconds


def _insurance_preview_payload(task_id, plan):
    """社保核算预览响应（v2.3.5）：待生效参数 + 份数清点 + 花名册概览

    对应「先确认参数再执行」：把将要生效的参数与文件范围一次性呈现，
    不执行任何 OCR；确认后由 confirm_task_id 启动识别。
    **不含命名解析与姓名匹配**——命名与统计规则归平台自身。
    """
    inv = plan.get('inventory') or {}
    ros = plan.get('roster') or {}
    warnings = []
    if plan.get('roster_error'):
        warnings.append('花名册解析失败：%s（确认后将不带花名册比对执行）'
                        % plan['roster_error'])
    return _ok({
        'task_id': task_id,
        'status': 'waiting_confirm',
        'message': '核算预览已生成，尚未开始识别，等待确认参数与文件范围',
        'user_confirmation_required': True,
        'effective_params': plan.get('effective_params') or {},
        'inventory': inv,
        'roster': ros,
        'warnings': warnings,
        'how_to_confirm': (
            '须先将以上 effective_params（省份/税种模式/统计年月/花名册）与 '
            'inventory / roster 清点结果呈现给用户，由用户确认或修正后，'
            '再调用 insurance_calculate 传 confirm_task_id=%s 启动识别；'
            '不得未经用户确认直接调用（v2.3.8 强制两阶段）。'
            '如需修正参数，可在确认调用中附带 province / tax_mode / '
            'year_start / month_start / year_end / month_end / roster_path '
            '覆盖预览值；大批量任务确认时可传 wait_seconds=600-1200'
            % task_id),
    })


def _insurance_confirm(confirm_id, args):
    """社保核算确认阶段（v2.3.5）：按预览暂存参数启动识别，args 可覆盖预览值"""
    task = tasks.get(confirm_id)
    if not task or task.get('capability') != 'insurance':
        return _err_payload('社保核算任务不存在: %s' % confirm_id,
                            {'task_id': confirm_id, 'capability': 'insurance',
                             'status': 'not_found'},
            suggestion='核对 task_id；预览阶段由 insurance_calculate 首次调用返回'
                       '（v2.3.8 起强制两阶段，首次调用恒为预览）')
    if task.get('status') != 'waiting_confirm':
        return _err_payload(
            '任务当前状态=%s，仅 waiting_confirm（待确认）状态可确认执行'
            % task.get('status'), task,
            suggestion='如需重新预览，请用 file_paths + province 重新提交'
                       '（v2.3.8 起首次调用恒为预览）')
    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        return _missing_payload([wait_issue], [])
    # 允许确认时覆盖预览参数：修正口径后直接执行，无需重新预览
    overrides = {}
    for k in ('province', 'tax_mode', 'roster_path'):
        if args.get(k) not in (None, ''):
            overrides[k] = args[k]
    present = [k for k in YEAR_KEYS if args.get(k) not in (None, '')]
    if present:
        if len(present) != len(YEAR_KEYS):
            missing = [k for k in YEAR_KEYS if k not in present]
            return _missing_payload([{
                'field': '/'.join(YEAR_KEYS),
                'issue': '统计时间段须四项同时提供，当前缺失: %s' % ', '.join(missing),
                'how_to_fix': '同时提供 %s（均为整数）；不改动则四项全部省略，'
                              '沿用预览值' % ', '.join(YEAR_KEYS),
            }], [])
        try:
            overrides['year_range'] = _year_range(args)
        except Exception as e:
            return _missing_payload([{
                'field': '/'.join(YEAR_KEYS),
                'issue': str(e),
                'how_to_fix': '年月均为整数，且起始不得晚于截止（如 2023-01 至 2025-12）',
            }], [])
    try:
        adapters.confirm_insurance(confirm_id, overrides)
    except Exception as e:
        return _err_payload('确认执行失败: %s' % e, task,
                            suggestion='任务可能已确认过或预览数据缺失，可重新预览')
    if wait_seconds > 0:
        return _wait_and_fetch(confirm_id, wait_seconds)
    return _immediate_payload(
        confirm_id, 'insurance', task.get('total') or 0,
        '社保核算已确认，识别进行中',
        effective={'confirm_task_id': confirm_id,
                   'overridden': sorted(overrides) or '无（沿用预览参数）',
                   'wait_seconds': wait_seconds})


# ---------------- 工具实现 ----------------

def tool_insurance_provinces(args):
    from modules.insurance.core import template_engine
    items = template_engine.get_provinces()
    return _ok({'provinces': [{'province_code': p.get('province_code'),
                               'province_name': p.get('province_name')}
                              for p in items]})


def tool_insurance_calculate(args):
    """社保智能核算（v2.3.8 起强制两阶段确认）。

    首次提交（无论是否传 preview_only）一律只做预览：清点文件、汇总
    待生效参数与花名册概览，不执行任何 OCR。调用方必须把预览内容呈现
    给用户，经用户确认（或修正参数）后凭 confirm_task_id 才能启动识别。
    执行入口只有 confirm_task_id 一条路——防止调用方 AI 跳过用户确认
    直接执行（2026-09-11 用户反馈确立）。
    """
    # 确认阶段（v2.3.5）：confirm_task_id 启动已预览的核算
    confirm_id = (args.get('confirm_task_id') or '').strip()
    if confirm_id:
        return _insurance_confirm(confirm_id, args)
    issues, optional, file_paths, province, tax_mode, year_range, wait_seconds = \
        _validate_insurance(args)
    if issues:
        return _missing_payload(issues, optional)
    # 强制两阶段（v2.3.8）：preview_only 参数仅为兼容保留（恒按预览处理），
    # 首次提交绝不直接执行；wait_seconds 由确认调用携带
    task_id = tasks.create('insurance', {
        'file_count': len(file_paths), 'province': province, 'tax_mode': tax_mode,
    })
    # 预览阶段（v2.3.5）：只清点文件与参数，不 OCR、不建原生任务
    try:
        plan = adapters.preview_insurance(
            task_id, file_paths, province, tax_mode=tax_mode,
            roster_path=args.get('roster_path'), year_range=year_range)
    except Exception as e:
        tasks.fail(task_id, e, '核算预览失败')
        return _err_payload('核算预览失败: %s' % e, tasks.get(task_id),
                            suggestion='核对文件路径与花名册后重新提交')
    return _insurance_preview_payload(task_id, plan)


def tool_convert_pdf_to_word(args):
    """PDF 转 Word（v2.3.8 起强制两阶段确认）。

    首次提交（无论是否传 preview_only）一律只返回文件清单预览，不执行任何
    转换；调用方必须把文件清单呈现给用户，经用户确认后凭 confirm_task_id
    才能执行。执行入口只有 confirm_task_id 一条路。
    """
    return _pdf2word_two_stage(args, 'pdf2word', PDF_EXTS)


def tool_convert_to_pdf(args):
    """其他格式转 PDF（v2.3.8 起强制两阶段确认）。

    首次提交一律只返回文件清单与输出方式预览（individual/merge 须由用户
    确认），不执行任何转换；经用户确认后凭 confirm_task_id 执行，确认时
    可附带 output_mode 覆盖输出方式。
    """
    return _pdf2word_two_stage(args, 'topdf', TOPDF_EXTS)


def _pdf2word_two_stage(args, direction, exts):
    """PDF 双向转换统一入口（v2.3.8 强制两阶段）：
    首次调用恒为预览（文件清点 + 输出方式），confirm_task_id 唯一执行入口"""
    confirm_id = (args.get('confirm_task_id') or '').strip()
    if confirm_id:
        return _pdf2word_confirm(confirm_id, args)
    issues, optional, file_paths, output_mode, wait_seconds = \
        _validate_pdf2word(args, exts, direction)
    if issues:
        return _missing_payload(issues, optional)
    task_id = tasks.create('pdf2word', {'file_count': len(file_paths),
                                        'direction': direction})
    # 预览阶段（v2.3.8）：只清点文件与输出方式，不执行任何转换
    tasks.update(task_id, status='waiting_confirm', total=len(file_paths),
                 message='转换预览已生成（待确认文件清单与输出方式）',
                 pending_pdf2word={
                     'file_paths': list(file_paths),
                     'direction': direction,
                     'output_mode': output_mode or 'individual',
                 })
    return _pdf2word_preview_payload(task_id, direction, file_paths,
                                     output_mode=output_mode)


def _pdf2word_preview_payload(task_id, direction, file_paths, output_mode=None):
    """转换预览响应（v2.3.8）：文件清单 + 输出方式，不执行任何转换

    转PDF的输出方式（individual 逐个 / merge 合并单PDF）是平台上需要用户
    选择的项，须在预览中呈现并由用户确认。
    """
    by_ext = {}
    for p in file_paths:
        ext = os.path.splitext(p)[1].lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
    eff = {'direction': direction, 'file_count': len(file_paths)}
    if direction == 'topdf':
        eff['output_mode'] = output_mode or 'individual'
    guide = (
        '须先将以上文件清单%s呈现给用户，由用户确认后，再调用 %s 传 '
        'confirm_task_id=%s 执行转换；不得未经用户确认直接调用'
        '（v2.3.8 强制两阶段）。'
        % ('与输出方式' if direction == 'topdf' else '',
           'convert_to_pdf' if direction == 'topdf' else 'convert_pdf_to_word',
           task_id))
    if direction == 'topdf':
        guide += ('输出方式可在确认时以 output_mode=individual（逐个输出）或 '
                  'output_mode=merge（合并为单个PDF）覆盖；大批量任务确认时可传 '
                  'wait_seconds=600-1200')
    return _ok({
        'task_id': task_id,
        'status': 'waiting_confirm',
        'message': '转换预览已生成，尚未执行任何转换，等待确认文件清单与输出方式',
        'user_confirmation_required': True,
        'effective_params': eff,
        'inventory': {
            'total': len(file_paths),
            'by_extension': by_ext,
            'files': [os.path.basename(p)
                      for p in file_paths[:PREVIEW_INLINE_LIMIT]],
            'files_truncated': len(file_paths) > PREVIEW_INLINE_LIMIT,
        },
        'how_to_confirm': guide,
    })


def _pdf2word_confirm(confirm_id, args):
    """转换确认阶段（v2.3.8）：按预览暂存的清单执行，args 可覆盖 output_mode"""
    task = tasks.get(confirm_id)
    if not task or task.get('capability') != 'pdf2word':
        return _err_payload('转换任务不存在: %s' % confirm_id,
                            {'task_id': confirm_id, 'capability': 'pdf2word',
                             'status': 'not_found'},
                            suggestion='核对 task_id；预览阶段由转换工具首次调用返回'
                                       '（v2.3.8 起强制两阶段，首次调用恒为预览）')
    if task.get('status') != 'waiting_confirm':
        return _err_payload(
            '任务当前状态=%s，仅 waiting_confirm（待确认）状态可确认执行'
            % task.get('status'), task,
            suggestion='如需重新预览，请用 file_paths 重新提交'
                       '（v2.3.8 起首次调用恒为预览）')
    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        return _missing_payload([wait_issue], [])
    pending = task.get('pending_pdf2word') or {}
    file_paths = pending.get('file_paths') or []
    direction = pending.get('direction') or 'pdf2word'
    output_mode = pending.get('output_mode') or 'individual'
    if not file_paths:
        return _err_payload('任务缺少待确认的预览数据，请重新提交 file_paths 生成预览',
                            task, suggestion='重新提交 file_paths 即可重新预览')
    if direction == 'topdf' and args.get('output_mode'):
        om = str(args['output_mode'])
        if om not in ('individual', 'merge'):
            return _missing_payload([{
                'field': 'output_mode',
                'issue': "output_mode 只能是 'individual' 或 'merge'，当前为: %s" % om,
                'how_to_fix': "逐个输出传 'individual'；合并为单个 PDF 传 'merge'；"
                              "不改动则省略，沿用预览值",
            }], [])
        output_mode = om
    tasks.update(confirm_id, pending_pdf2word=None)
    try:
        adapters.start_pdf2word(confirm_id, file_paths, direction=direction,
                                output_mode=output_mode)
    except Exception as e:
        return _err_payload('确认执行失败: %s' % e, task,
                            suggestion='可重新提交 file_paths 生成新预览')
    effective = {'confirm_task_id': confirm_id, 'direction': direction,
                 'wait_seconds': wait_seconds}
    if direction == 'topdf':
        effective['output_mode'] = output_mode
    if wait_seconds > 0:
        return _wait_and_fetch(confirm_id, wait_seconds)
    return _immediate_payload(
        confirm_id, 'pdf2word', len(file_paths),
        '转换已确认，执行中',
        effective=effective)


# 合同预览响应中各清单的内联上限（完整计划一律落盘，见 artifacts 卡片）
PREVIEW_INLINE_LIMIT = 100


def _contract_preview_payload(task_id, plan, roster_path=None):
    """预览阶段响应：重名待确认（含候选归属人）等需人工选择项内联展示，
    完整计划（含全量 auto 清单）落盘为卡片，不执行任何重命名。"""
    dups = plan.get('duplicates', [])
    unmatched = plan.get('unmatched', [])
    auto = plan.get('auto', [])
    card = artifacts.write_json('contract', task_id, 'preview', plan,
                                label='重命名计划预览（完整JSON，含全部自动匹配项）')
    return _ok({
        'task_id': task_id,
        'status': 'waiting_confirm',
        'message': '重命名计划已生成，未执行任何重命名，等待确认',
        'user_confirmation_required': True,
        'effective_params': {'roster_path': roster_path,
                             'file_count': plan.get('total')},
        'summary': {
            'total': plan.get('total'),
            'auto': len(auto),
            'duplicates': len(dups),
            'unmatched': len(unmatched),
            'roster_missing': len(plan.get('roster_missing', [])),
        },
        # 平台「重名待确认（请选择归属人）」环节对应的待选择项
        'needs_selection': [
            {'original': d.get('original') or d.get('basename'),
             'guessed': d.get('guessed'),
             'reason': d.get('reason'),
             'candidates': d.get('candidates', [])}
            for d in dups[:PREVIEW_INLINE_LIMIT]
        ],
        'needs_selection_truncated': len(dups) > PREVIEW_INLINE_LIMIT,
        'unmatched': [
            {'original': u.get('original'), 'guessed': u.get('guessed'),
             'reason': u.get('reason')}
            for u in unmatched[:PREVIEW_INLINE_LIMIT]
        ],
        'unmatched_truncated': len(unmatched) > PREVIEW_INLINE_LIMIT,
        'roster_missing': plan.get('roster_missing', []),
        'auto_preview': [
            {'original': a.get('original'), 'new_name': a.get('new_name')}
            for a in auto[:PREVIEW_INLINE_LIMIT]
        ],
        'auto_preview_truncated': len(auto) > PREVIEW_INLINE_LIMIT,
        'artifacts': [card],
        'how_to_confirm': (
            '须先将以上重命名计划（summary / needs_selection / auto_preview）'
            '呈现给用户，由用户对重名项做出选择并确认后，再调用 contract_organize '
            '传 confirm_task_id=%s 执行重命名；不得未经用户确认直接调用'
            '（v2.3.8 强制两阶段）。'
            'choices 数组中：重名项传 {"original": 原文件名, "seq": 候选人的 seq}'
            '（seq 取自 candidates），可选加 "new_name" 覆盖新文件名；'
            'auto 项也可传 {"original", "new_name"} 改名；'
            '未出现在 choices 中的重名项与未匹配文件将移入「待处理」目录'
            % task_id),
    })


def _contract_confirm(confirm_id, args):
    """确认阶段：校验任务状态与 choices，启动执行线程"""
    task = tasks.get(confirm_id)
    if not task or task.get('capability') != 'contract':
        return _err_payload('合同整理任务不存在: %s' % confirm_id,
                            {'task_id': confirm_id, 'capability': 'contract',
                             'status': 'not_found'},
            suggestion='核对 task_id；预览阶段由 contract_organize 首次调用返回'
                       '（v2.3.8 起强制两阶段，首次调用恒为生成计划）')
    if task.get('status') != 'waiting_confirm':
        return _err_payload(
            '任务当前状态=%s，仅 waiting_confirm（待确认）状态可确认执行'
            % task.get('status'), task,
            suggestion='如需重新预览，请用 file_paths + roster_path 重新提交'
                       '（v2.3.8 起首次调用恒为生成计划）')
    choices = args.get('choices') or []
    if not isinstance(choices, list):
        return _missing_payload([{
            'field': 'choices',
            'issue': 'choices 必须是数组，元素为 {"original", "seq"?, "new_name"?}',
            'how_to_fix': '重名项传 original+seq（seq 取自预览响应 candidates）；'
                          '改名传 original+new_name；不需要调整时省略 choices',
        }], [])
    wait_issue, wait_seconds = _check_wait(args)
    if wait_issue:
        return _missing_payload([wait_issue], [])
    try:
        adapters.confirm_contract(confirm_id, choices)
    except Exception as e:
        return _err_payload('确认执行失败: %s' % e, task,
                            suggestion='任务可能已确认过或计划数据缺失，可重新预览')
    if wait_seconds > 0:
        return _wait_and_fetch(confirm_id, wait_seconds)
    return _immediate_payload(
        confirm_id, 'contract', task.get('total') or 0,
        '合同整理已确认，重命名执行中',
        effective={'confirm_task_id': confirm_id,
                   'choices_count': len(choices),
                   'wait_seconds': wait_seconds})


def tool_contract_organize(args):
    """合同整理（v2.3.8 起强制两阶段确认）。

    首次提交（无论是否传 preview_only）一律只生成重命名计划与「重名待确认」
    清单，不执行任何重命名；调用方必须把计划呈现给用户，由用户做出选择并
    确认后，凭 confirm_task_id + choices 执行。执行入口只有 confirm_task_id
    一条路——不再提供无人值守直接执行（2026-09-11 用户反馈确立）。
    """
    # 确认阶段（v2.3.2）：confirm_task_id + choices 执行已预览的计划
    confirm_id = (args.get('confirm_task_id') or '').strip()
    if confirm_id:
        return _contract_confirm(confirm_id, args)
    issues, optional, file_paths, roster_path, wait_seconds = \
        _validate_contract(args)
    if issues:
        return _missing_payload(issues, optional)
    # 强制两阶段（v2.3.8）：preview_only 参数仅为兼容保留（恒按预览处理）
    task_id = tasks.create('contract', {'file_count': len(file_paths),
                                        'preview_only': True})
    # 预览阶段（v2.3.2）：同步生成计划，返回需人工选择项，不执行重命名
    try:
        plan = adapters.preview_contract(task_id, file_paths, roster_path)
    except Exception as e:
        tasks.fail(task_id, e, '计划生成失败')
        return _err_payload('计划生成失败: %s' % e, tasks.get(task_id),
                            suggestion='核对花名册与文件路径后重新提交')
    return _contract_preview_payload(task_id, plan, roster_path=roster_path)


def tool_get_task_status(args):
    """查询进度：只给精简视图（状态/进度/耗时），结果明细一律不内联"""
    task_id = (args.get('task_id') or '').strip()
    task = tasks.get(task_id)
    if not task:
        raise ValueError('任务不存在: %s' % task_id)
    task = _sync_insurance(task)
    view = tasks.public_view(task)
    view.pop('result', None)          # 明细不进响应，仅由 get_task_result 给卡片
    sec = _elapsed(task)
    view['elapsed_sec'] = sec
    view['elapsed_human'] = _elapsed_human(sec)
    if task.get('status') in ('pending', 'processing') and (sec or 0) > STALE_AFTER_SEC:
        view['stale'] = True
        view['stale_hint'] = ('任务已运行 %s 仍在进行，超过 %d 分钟可视为异常；'
                              '可继续轮询或重新提交' % (_elapsed_human(sec),
                                                  STALE_AFTER_SEC // 60))
    else:
        view['stale'] = False
    if task.get('status') == 'waiting_confirm':
        if task.get('capability') == 'insurance':
            view['hint'] = ('核算待确认：调用 insurance_calculate 传 confirm_task_id 确认执行；'
                            '待核对内容见预览响应的 effective_params/inventory/roster')
        elif task.get('capability') == 'pdf2word':
            direction = (task.get('pending_pdf2word') or {}).get('direction')
            tool_name = 'convert_to_pdf' if direction == 'topdf' else 'convert_pdf_to_word'
            view['hint'] = ('转换待确认：调用 %s 传 confirm_task_id 确认执行'
                            '（转PDF可在确认时以 output_mode 覆盖输出方式）；'
                            '待核对内容见预览响应的 effective_params/inventory' % tool_name)
        else:
            view['hint'] = ('计划待确认：调用 contract_organize 传 confirm_task_id 与 '
                            'choices 确认执行；待选择项见预览响应 needs_selection')
    else:
        view['hint'] = '完成后调用 get_task_result 取精简摘要与文件卡片'
    return _ok(view)


def tool_get_task_result(args):
    """取结果：精简摘要 + 结果规模 + 文件卡片；未完成/失败给出明确降级信息"""
    task_id = (args.get('task_id') or '').strip()
    task = tasks.get(task_id)
    if not task:
        raise ValueError('任务不存在: %s' % task_id)
    task = _sync_insurance(task)
    status = task.get('status')

    # 降级一：任务失败 —— 明确错误 + 错误报告卡片 + 建议
    if status == 'error':
        return _err_payload(
            task.get('error') or '任务执行失败', task,
            suggestion='错误报告已落盘（见 artifacts）；请核对入参与文件后重新提交')

    # 待确认（两阶段能力分流）：计划/预览已生成但未执行，引导确认
    if status == 'waiting_confirm':
        cap = task.get('capability')
        if cap == 'pdf2word':
            direction = (task.get('pending_pdf2word') or {}).get('direction')
            tool_name = 'convert_to_pdf' if direction == 'topdf' else 'convert_pdf_to_word'
            return _err_payload(
                '任务待确认：转换预览已生成，尚未执行任何转换', task,
                suggestion='调用 %s 传 confirm_task_id=%s 确认执行'
                           '（转PDF可在确认时以 output_mode 覆盖输出方式）；'
                           '待核对内容见预览响应的 effective_params/inventory'
                           % (tool_name, task_id))
        if cap == 'insurance':
            return _err_payload(
                '任务待确认：核算预览已生成，尚未开始识别', task,
                suggestion='调用 insurance_calculate 传 confirm_task_id=%s 确认执行'
                           '（确认时可覆盖 province/tax_mode/年份月份/roster_path）；'
                           '待核对内容见预览响应的 effective_params/inventory/roster'
                           % task_id)
        return _err_payload(
            '任务待确认：重命名计划已生成，尚未执行任何重命名', task,
            suggestion='调用 contract_organize 传 confirm_task_id=%s 与 choices '
                       '确认执行；待选择项（重名归属，含 candidates）见预览响应的 '
                       'needs_selection 与落盘计划卡片' % task_id)

    # 降级二：任务未完成 —— 明确状态、进度与耗时，并给出超时判定
    if status in ('pending', 'processing') or not _artifacts_of(task):
        sec = _elapsed(task)
        msg = ('任务尚未完成：状态=%s，进度=%s/%s，%s，已运行 %s'
               % (status, task.get('current'), task.get('total'),
                  task.get('message') or '', _elapsed_human(sec)))
        suggestion = '请稍后再次调用 get_task_status 轮询，完成后再取结果'
        if (sec or 0) > STALE_AFTER_SEC:
            suggestion = ('已运行 %s 超过 %d 分钟仍无结果，可视为超时；'
                          '建议核对文件数量后重新提交，或联系管理员查看日志'
                          % (_elapsed_human(sec), STALE_AFTER_SEC // 60))
        return _err_payload(msg, task, suggestion=suggestion)

    return _ok(_thin_payload(task))


TOOLS = {
    'insurance_provinces': {
        'description': '查询社保智能核算当前支持的参保证明省份列表（调用社保核算前必须先取得省份代码）',
        'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
        'handler': tool_insurance_provinces,
    },
    'insurance_calculate': {
        'description': '社保智能核算：批量识别参保证明（图片/PDF），生成重点群体参保统计与台账Excel。'
                       '强制两阶段确认（v2.3.8）：首次调用一律只返回「待生效参数 + 文件清点 + 花名册概览」'
                       '且不执行任何 OCR——必须把预览内容呈现给用户，经用户确认或修正参数后，'
                       '再用 confirm_task_id 确认执行（确认时可在同一次调用中覆盖 '
                       'province/tax_mode/年份月份/roster_path）；不存在跳过用户确认的直接执行。'
                       '长耗时任务；确认时可传 wait_seconds 由 Server 内部等待并一次性返回全部结果'
                       '（数百份以上建议 600-1200，避免反复轮询）。'
                       '结果与Excel均落盘，响应只回精简摘要与文件卡片（逐人参保明细含身份证号，不随响应返回）。'
                       '入参缺失时一次性返回全部 blocking 项与可选项默认值（候选值须交用户选择）。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '参保证明文件路径数组，支持图片(jpg/png/bmp/tif)与PDF，可填目录（自动递归）'},
                'province': {'type': 'string',
                             'description': '参保证明所属省份代码（必填，先用 insurance_provinces 查询；候选值须交用户选择）'},
                'tax_mode': {'type': 'string', 'enum': ['退税', '抵税'],
                             'description': '税种模式，默认退税（须与用户确认）'},
                'roster_path': {'type': 'string', 'description': '花名册文件路径（可选，用于人员比对）'},
                'year_start': {'type': 'integer', 'description': '统计起始年（与其余三个时间参数一并填写）'},
                'month_start': {'type': 'integer', 'description': '统计起始月'},
                'year_end': {'type': 'integer', 'description': '统计截止年'},
                'month_end': {'type': 'integer', 'description': '统计截止月'},
                'preview_only': {'type': 'boolean',
                                 'description': '兼容保留（v2.3.8 起强制两阶段：首次调用恒为预览，'
                                                '不执行任何 OCR；核对后用 confirm_task_id 确认执行）'},
                'confirm_task_id': {'type': 'string',
                                    'description': '确认执行：预览返回的 task_id（须在用户核对确认后调用）。'
                                                   '传此参数时 file_paths/province 不需要；'
                                                   '可同时附带 province/tax_mode/年份月份/roster_path 覆盖预览值'},
                'wait_seconds': {'type': 'integer',
                                 'description': 'Server 内部等待并一次性返回结果的秒数（0-%d，默认 0 立即返回 task_id）。'
                                                '大批量任务建议 600-1200' % MAX_WAIT_SEC},
            },
            'required': [],
        },
        'handler': tool_insurance_calculate,
    },
    'convert_pdf_to_word': {
        'description': 'PDF 转 Word：批量将 PDF 转换为可编辑 docx，保持原排版（A4规范化+逐页方向保持+逐项校验）。'
                       '强制两阶段确认（v2.3.8）：首次调用一律只返回文件清单预览，不执行任何转换——'
                       '必须把文件清单呈现给用户，由用户确认后再用 confirm_task_id 执行；'
                       '不存在跳过用户确认的直接执行。'
                       '长耗时任务；确认时可传 wait_seconds 由 Server 内部等待并一次性返回全部结果。'
                       '结果落盘，响应只回精简摘要与文件卡片。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': 'PDF 文件路径数组，可填目录（自动递归）'},
                'confirm_task_id': {'type': 'string',
                                    'description': '确认执行：预览返回的 task_id（须在用户核对文件清单后调用）。'
                                                   '传此参数时 file_paths 不需要'},
                'wait_seconds': {'type': 'integer',
                                 'description': 'Server 内部等待并一次性返回结果的秒数（0-%d，默认 0 立即返回 task_id）。'
                                                '大批量任务建议 600-1200' % MAX_WAIT_SEC},
            },
            'required': [],
        },
        'handler': tool_convert_pdf_to_word,
    },
    'convert_to_pdf': {
        'description': '其他格式转 PDF：支持 Word/Excel/图片/文本/PDF，统一输出 A4 并按主体内容判定横竖版。'
                       '强制两阶段确认（v2.3.8）：首次调用一律只返回文件清单与输出方式预览，不执行任何转换——'
                       '必须把清单与输出方式（individual/merge）呈现给用户，由用户确认后再用 '
                       'confirm_task_id 执行（确认时可附 output_mode 覆盖）；不存在跳过用户确认的直接执行。'
                       '长耗时任务；确认时可传 wait_seconds 由 Server 内部等待并一次性返回全部结果。'
                       '结果落盘，响应只回精简摘要与文件卡片。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '待转换文件路径数组（docx/doc/xlsx/xls/图片/txt/md/pdf），可填目录'},
                'output_mode': {'type': 'string', 'enum': ['individual', 'merge'],
                                'description': "individual=逐个输出（默认），merge=合并为单个PDF（合并顺序=清单顺序，呈现给用户时须确认排序）"},
                'confirm_task_id': {'type': 'string',
                                    'description': '确认执行：预览返回的 task_id（须在用户核对清单与输出方式后调用）。'
                                                   '传此参数时 file_paths 不需要；可同时附 output_mode 覆盖预览值'},
                'wait_seconds': {'type': 'integer',
                                 'description': 'Server 内部等待并一次性返回结果的秒数（0-%d，默认 0 立即返回 task_id）。'
                                                '大批量任务建议 600-1200' % MAX_WAIT_SEC},
            },
            'required': [],
        },
        'handler': tool_convert_to_pdf,
    },
    'contract_organize': {
        'description': '劳动合同整理：按花名册智能匹配合同影像并批量重命名归档。'
                       '强制两阶段确认（v2.3.8）：首次调用一律只返回重命名计划与「重名待确认」清单'
                       '（含候选归属人 candidates），不执行任何重命名——必须把计划呈现给用户，'
                       '由用户选择并用 confirm_task_id + choices 确认执行，'
                       '未选择的重名项与未匹配文件移入「待处理」目录；不存在跳过用户确认的直接执行。'
                       '长耗时任务；确认时可传 wait_seconds 由 Server 内部等待并一次性返回全部结果。'
                       '结果落盘，响应只回精简摘要与文件卡片。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '合同影像文件路径数组（图片/PDF），可填目录（自动递归）'},
                'roster_path': {'type': 'string',
                                'description': '花名册文件路径（Excel/CSV），用于姓名匹配'},
                'preview_only': {'type': 'boolean',
                                 'description': '兼容保留（v2.3.8 起强制两阶段：首次调用恒为生成计划，'
                                                '不执行任何重命名；用户选择后用 confirm_task_id + choices 确认执行）'},
                'confirm_task_id': {'type': 'string',
                                    'description': '确认执行：预览返回的 task_id（须在用户核对选择后调用）。'
                                                   '传此参数时 file_paths/roster_path 不需要'},
                'choices': {'type': 'array',
                            'items': {'type': 'object'},
                            'description': '确认选择（配合 confirm_task_id）：重名项传 {"original", "seq"}'
                                           '（seq 取自预览响应 candidates）；任意项可加 "new_name" 覆盖新文件名；'
                                           '省略时重名项全部进「待处理」'},
                'wait_seconds': {'type': 'integer',
                                 'description': 'Server 内部等待并一次性返回结果的秒数（0-%d，默认 0 立即返回 task_id）。'
                                                '大批量任务建议 600-1200' % MAX_WAIT_SEC},
            },
            'required': [],
        },
        'handler': tool_contract_organize,
    },
    'get_task_status': {
        'description': '查询任务进度与状态（status: pending/processing/success/error）。'
                       '只返回精简视图（进度与耗时），不含结果明细。',
        'inputSchema': {
            'type': 'object',
            'properties': {'task_id': {'type': 'string', 'description': '任务ID'}},
            'required': ['task_id'],
        },
        'handler': tool_get_task_status,
    },
    'get_task_result': {
        'description': '获取已完成任务的结果：精简摘要（统计口径）+ 结果规模（文件数/字节）+ '
                       '文件卡片（name/path/uri/size_human/lines/mime），可直接查看、下载或打开。'
                       '完整明细不随响应返回，已落盘为 JSON。任务未完成或失败时返回明确错误信息与后续建议。',
        'inputSchema': {
            'type': 'object',
            'properties': {'task_id': {'type': 'string', 'description': '任务ID'}},
            'required': ['task_id'],
        },
        'handler': tool_get_task_result,
    },
}


def call_tool(name, arguments):
    """执行工具，返回 (text, is_error)"""
    tool = TOOLS.get(name)
    if not tool:
        return _err('未知工具: %s' % name)
    try:
        return tool['handler'](arguments or {})
    except Exception as e:
        return _err('%s' % e)


def list_tools():
    """返回 MCP tools/list 所需的工具描述列表"""
    return [{
        'name': name,
        'description': spec['description'],
        'inputSchema': spec['inputSchema'],
    } for name, spec in TOOLS.items()]
