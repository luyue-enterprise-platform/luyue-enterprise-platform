# -*- coding: utf-8 -*-
"""MCP 结果落盘与文件卡片（瘦返回交付模式）

交付约定
--------
1. **瘦返回**：工具响应只回「精简摘要 + 执行状态 + 结果规模 + 文件引用」，
   绝不内联大段内容（批量明细、逐人参保记录、逐图识别详情等一律不进响应）。
2. **完整结果落盘**：写入 ``<数据目录>/outputs/mcp/<能力>/<任务ID>/``，
   文件名 ``mcp-<能力>-<任务ID>-<YYYYmmdd-HHMMSS>[-n].json``，
   带可读前缀、任务标识与时间戳，同任务多次产出**不覆盖历史结果**。
3. **文件卡片**：响应中以卡片形式给出落盘结果与输出目录中的产出文件，
   含 name / path / uri / size / lines / mime，便于直接查看、下载或打开。
4. **降级明确**：错误与超时场景落盘错误报告，并在响应中给出
   明确错误信息、当前状态与后续建议。

安全说明
--------
社保 result 含 person_stats / image_details（逐人、逐图，且带身份证号），
属于典型的大体量 + 敏感字段，必须落盘而非内联回传。
"""
import json
import os
import urllib.parse
from datetime import datetime

from core.paths import data_dir

# 输出目录中的产出文件最多生成多少张卡片（其余只报总数，避免响应膨胀）
OUTPUT_CARD_LIMIT = 20

# 文本类文件才统计行数；超过此大小不再读行（避免读大文件拖慢响应）
_MAX_LINE_SCAN_BYTES = 20 * 1024 * 1024

_MIME = {
    '.json': 'application/json',
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.doc': 'application/msword',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.bmp': 'image/bmp',
    '.gif': 'image/gif',
    '.tif': 'image/tiff',
    '.tiff': 'image/tiff',
    '.webp': 'image/webp',
}

_TEXT_EXT = {'.json', '.txt', '.md', '.csv', '.log'}

# 落盘文件名模板：mcp-<能力>-<任务ID>-<前缀>-<时间戳>[-n].<扩展名>
# （具名常量：既是命名规则的唯一出处，也便于打包后进行字节码针刺校验）
NAME_TEMPLATE = 'mcp-%s-%s-%s-%s.%s'


def _now():
    return datetime.now()


def stamp():
    """时间戳片段：YYYYmmdd-HHMMSS"""
    return _now().strftime('%Y%m%d-%H%M%S')


def _unique_path(path):
    """同名则追加 -2 -3 ...，保证不覆盖历史结果"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists('%s-%d%s' % (base, i, ext)):
        i += 1
    return '%s-%d%s' % (base, i, ext)


def human_size(n):
    """字节数转人类可读"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '-'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.0f %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return '%.1f GB' % n


def mime_of(path):
    return _MIME.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')


def file_uri(path):
    """本地文件的 file:// URI（中文/空格需百分号编码，否则打不开）"""
    p = os.path.abspath(path).replace('\\', '/')
    if not p.startswith('/'):
        p = '/' + p
    return 'file://' + urllib.parse.quote(p)


def _count_lines(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in _TEXT_EXT:
        return None
    try:
        if os.path.getsize(path) > _MAX_LINE_SCAN_BYTES:
            return None
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def make_card(path, kind='output', label=None, note=None):
    """生成文件卡片：AI 可据此展示/打开/下载"""
    path = os.path.abspath(path)
    try:
        st = os.stat(path)
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')
    except Exception:
        size, mtime = None, None
    name = os.path.basename(path)
    card = {
        'name': name,
        'kind': kind,                      # result=完整结果 / output=产出文件 / error=错误报告
        'mime': mime_of(path),
        'path': path,
        'uri': file_uri(path),
        'size_bytes': size,
        'size_human': human_size(size) if size is not None else '-',
        'modified_at': mtime,
    }
    if size is not None:
        card['lines'] = _count_lines(path)
    if label:
        card['label'] = label
    if note:
        card['note'] = note
    return card


def artifact_dir(capability, task_id):
    """结果落盘目录：数据目录/outputs/mcp/<能力>/<任务ID>"""
    path = os.path.join(data_dir(), 'outputs', 'mcp', capability, task_id)
    os.makedirs(path, exist_ok=True)
    return path


def build_name(capability, task_id, prefix, ext='json'):
    """落盘文件名：mcp-<能力>-<任务ID>-<前缀>-<时间戳>.json（可读 + 可追溯 + 不覆盖）"""
    return NAME_TEMPLATE % (capability, task_id, prefix, stamp(), ext.lstrip('.'))


def write_json(capability, task_id, prefix, payload, kind='result', label=None):
    """完整结果落盘，返回文件卡片"""
    directory = artifact_dir(capability, task_id)
    path = _unique_path(os.path.join(directory, build_name(capability, task_id, prefix)))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return make_card(path, kind=kind, label=label or '完整结果（JSON）')


def write_error(capability, task_id, message, detail=None):
    """错误报告落盘，返回文件卡片（降级场景仍留痕可追溯）"""
    return write_json(capability, task_id, 'error', {
        'task_id': task_id,
        'capability': capability,
        'error': str(message),
        'detail': detail,
        'created_at': _now().isoformat(timespec='seconds'),
    }, kind='error', label='错误报告（JSON）')


def collect_output_cards(out_dir, limit=OUTPUT_CARD_LIMIT):
    """扫描输出目录，为产出文件生成卡片（超出 limit 的只报总数）

    返回 (cards, total, truncated)
    """
    cards, total = [], 0
    if not out_dir or not os.path.isdir(out_dir):
        return cards, 0, False
    for root, _dirs, files in os.walk(out_dir):
        for fn in sorted(files):
            # 结果 JSON 已由 write_json 单独出卡片，此处不重复
            if fn.startswith('mcp-') and fn.endswith('.json'):
                continue
            total += 1
            if len(cards) < limit:
                rel = os.path.relpath(os.path.join(root, fn), out_dir)
                cards.append(make_card(os.path.join(root, fn), kind='output',
                                       label=rel.replace('\\', '/')))
    return cards, total, total > len(cards)


def slim(summary, drop_keys=('files', 'detail', 'image_details', 'person_stats')):
    """裁剪掉大段明细，只留统计字段（用于响应内联）"""
    if not isinstance(summary, dict):
        return summary
    out = {k: v for k, v in summary.items() if k not in drop_keys}
    # 明细数量以计数形式保留，保证摘要信息不丢失
    for k in drop_keys:
        if isinstance(summary.get(k), list):
            out['%s_count' % k] = len(summary[k])
    return out


def result_size(cards):
    """按卡片汇总结果规模：文件数 + 总字节 + 是否截断"""
    total = 0
    for c in cards or []:
        if c.get('size_bytes'):
            total += c['size_bytes']
    return {'files': len(cards or []), 'bytes': total, 'size_human': human_size(total)}
