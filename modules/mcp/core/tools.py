# -*- coding: utf-8 -*-
"""MCP 工具定义

每个工具 = 名称 + 说明 + JSON Schema 入参 + 处理器。
处理器统一返回 (text, is_error)：text 为回传给 AI 的可读文本（JSON），
is_error=True 时按 MCP 规范标记工具执行失败。

批量能力均为长耗时操作，统一返回 task_id，由 get_task_status / get_task_result
完成后续轮询与取结果。
"""
import json
import os

from . import adapters, tasks

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


# ---------------- 工具实现 ----------------

def tool_insurance_provinces(args):
    from modules.insurance.core import template_engine
    items = template_engine.get_provinces()
    return _ok({'provinces': [{'province_code': p.get('province_code'),
                               'province_name': p.get('province_name')}
                              for p in items]})


def tool_insurance_calculate(args):
    file_paths = _expand(args.get('file_paths'), INSURANCE_EXTS)
    province = (args.get('province') or '').strip()
    if not province:
        raise ValueError('province 为必填项（可先调用 insurance_provinces 查询）')
    year_range = _year_range(args)
    task_id = tasks.create('insurance', {
        'file_count': len(file_paths), 'province': province,
        'tax_mode': args.get('tax_mode', '退税'),
    })
    adapters.start_insurance(task_id, file_paths, province,
                             tax_mode=args.get('tax_mode', '退税'),
                             roster_path=args.get('roster_path'),
                             year_range=year_range)
    return _ok({'task_id': task_id, 'file_count': len(file_paths),
                'message': '社保核算任务已提交，用 get_task_status 查询进度'})


def tool_convert_pdf_to_word(args):
    file_paths = _expand(args.get('file_paths'), PDF_EXTS)
    task_id = tasks.create('pdf2word', {'file_count': len(file_paths),
                                        'direction': 'pdf2word'})
    adapters.start_pdf2word(task_id, file_paths, direction='pdf2word')
    return _ok({'task_id': task_id, 'file_count': len(file_paths),
                'message': 'PDF转Word任务已提交，用 get_task_status 查询进度'})


def tool_convert_to_pdf(args):
    file_paths = _expand(args.get('file_paths'), TOPDF_EXTS)
    output_mode = args.get('output_mode') or 'individual'
    if output_mode not in ('individual', 'merge'):
        raise ValueError("output_mode 只能是 'individual' 或 'merge'")
    task_id = tasks.create('pdf2word', {'file_count': len(file_paths),
                                        'direction': 'topdf',
                                        'output_mode': output_mode})
    adapters.start_pdf2word(task_id, file_paths, direction='topdf',
                            output_mode=output_mode)
    return _ok({'task_id': task_id, 'file_count': len(file_paths),
                'message': '转PDF任务已提交，用 get_task_status 查询进度'})


def tool_contract_organize(args):
    file_paths = _expand(args.get('file_paths'), CONTRACT_EXTS)
    roster_path = args.get('roster_path')
    if roster_path and not os.path.isfile(roster_path):
        raise ValueError('花名册文件不存在: %s' % roster_path)
    task_id = tasks.create('contract', {'file_count': len(file_paths)})
    adapters.start_contract(task_id, file_paths, roster_path=roster_path)
    return _ok({'task_id': task_id, 'file_count': len(file_paths),
                'message': '合同整理任务已提交，用 get_task_status 查询进度'})


def tool_get_task_status(args):
    task_id = (args.get('task_id') or '').strip()
    task = tasks.get(task_id)
    if not task:
        raise ValueError('任务不存在: %s' % task_id)
    # 社保能力复用宿模块原生任务表，实时回读真实进度
    if task.get('capability') == 'insurance' and task.get('native_task_id'):
        native = adapters.insurance_native(task['native_task_id']) or {}
        task.update({
            'status': native.get('status', task.get('status')),
            'current': native.get('current', task.get('current')),
            'total': native.get('total', task.get('total')),
            'message': native.get('message', task.get('message')),
        })
        if native.get('result') is not None:
            task['result'] = native.get('result')
            task['status'] = 'success'
    return _ok(tasks.public_view(task))


def tool_get_task_result(args):
    task_id = (args.get('task_id') or '').strip()
    task = tasks.get(task_id)
    if not task:
        raise ValueError('任务不存在: %s' % task_id)
    if task.get('capability') == 'insurance' and task.get('native_task_id'):
        native = adapters.insurance_native(task['native_task_id']) or {}
        if native.get('result') is not None:
            task['result'] = native.get('result')
            task['status'] = native.get('status', 'success')
    if task.get('status') == 'error':
        raise ValueError(task.get('error') or '任务执行失败')
    if task.get('result') is None:
        raise ValueError('任务尚未完成（当前状态：%s，%s）'
                         % (task.get('status'), task.get('message')))
    return _ok({'task_id': task_id, 'status': task.get('status'),
                'result': task.get('result')})


TOOLS = {
    'insurance_provinces': {
        'description': '查询社保智能核算当前支持的参保证明省份列表（调用社保核算前必须先取得省份代码）',
        'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
        'handler': tool_insurance_provinces,
    },
    'insurance_calculate': {
        'description': '社保智能核算：批量识别参保证明（图片/PDF），生成重点群体参保统计与台账Excel。'
                       '长耗时任务，返回 task_id，需用 get_task_status 轮询、get_task_result 取结果。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '参保证明文件路径数组，支持图片(jpg/png/bmp/tif)与PDF，可填目录（自动递归）'},
                'province': {'type': 'string',
                             'description': '参保证明所属省份代码（必填，先用 insurance_provinces 查询）'},
                'tax_mode': {'type': 'string', 'enum': ['退税', '抵税'],
                             'description': '税种模式，默认退税'},
                'roster_path': {'type': 'string', 'description': '花名册文件路径（可选，用于人员比对）'},
                'year_start': {'type': 'integer', 'description': '统计起始年（与其余三个时间参数一并填写）'},
                'month_start': {'type': 'integer', 'description': '统计起始月'},
                'year_end': {'type': 'integer', 'description': '统计截止年'},
                'month_end': {'type': 'integer', 'description': '统计截止月'},
            },
            'required': ['file_paths', 'province'],
        },
        'handler': tool_insurance_calculate,
    },
    'convert_pdf_to_word': {
        'description': 'PDF 转 Word：批量将 PDF 转换为可编辑 docx，保持原排版（A4规范化+逐页方向保持+逐项校验）。'
                       '长耗时任务，返回 task_id。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': 'PDF 文件路径数组，可填目录（自动递归）'},
            },
            'required': ['file_paths'],
        },
        'handler': tool_convert_pdf_to_word,
    },
    'convert_to_pdf': {
        'description': '其他格式转 PDF：支持 Word/Excel/图片/文本/PDF，统一输出 A4 并按主体内容判定横竖版。'
                       '长耗时任务，返回 task_id。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '待转换文件路径数组（docx/doc/xlsx/xls/图片/txt/md/pdf），可填目录'},
                'output_mode': {'type': 'string', 'enum': ['individual', 'merge'],
                                'description': "individual=逐个输出（默认），merge=合并为单个PDF"},
            },
            'required': ['file_paths'],
        },
        'handler': tool_convert_to_pdf,
    },
    'contract_organize': {
        'description': '劳动合同整理：按花名册智能匹配合同影像并批量重命名归档；'
                       '自动匹配的直接重命名，未匹配与重名待确认的移入「待处理」目录。'
                       '长耗时任务，返回 task_id。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_paths': {'type': 'array', 'items': {'type': 'string'},
                               'description': '合同影像文件路径数组（图片/PDF），可填目录（自动递归）'},
                'roster_path': {'type': 'string',
                                'description': '花名册文件路径（Excel/CSV），用于姓名匹配'},
            },
            'required': ['file_paths', 'roster_path'],
        },
        'handler': tool_contract_organize,
    },
    'get_task_status': {
        'description': '查询任务进度与状态（status: pending/processing/success/error）。',
        'inputSchema': {
            'type': 'object',
            'properties': {'task_id': {'type': 'string', 'description': '任务ID'}},
            'required': ['task_id'],
        },
        'handler': tool_get_task_status,
    },
    'get_task_result': {
        'description': '获取已完成任务的结果（输出目录、成功/失败统计、明细）。任务未完成时报错。',
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
