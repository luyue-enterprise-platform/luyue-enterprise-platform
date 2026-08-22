"""文件重命名模块 — 按花名册对劳动合同文件智能匹配、冲突确认与批量重命名

流程:
  1. 姓名提取: 仅从图片文件自身的文件名提取候选姓名（容错空格、分隔符、序号、业务词；
     不使用文件夹名、工号、身份证号等其他来源或字段）
  2. 精确匹配: 候选姓名与花名册"姓名"字段为唯一匹配依据，不参考工号/身份证号/部门等
     其他字段；比对时忽略姓名前后空格（含全角空格）及全角/半角字符差异
  3. 冲突检测: 花名册存在同名员工、或文件名命中多个姓名 → 标记为冲突项，暂停待人工确认
  4. 重命名:   命中文件按 NAME_TEMPLATE 模板批量重命名（支持 {seq} {name} {idcard}）
  5. 待处理:   匹配失败文件移入"待处理"文件夹，并生成失败明细报告（Excel）
  6. 可追溯:   处理日志（JSON）记录每个文件 原文件名 -> 新文件名 及成败原因，支持回溯
"""

import os
import re
import json
import shutil
import logging
import unicodedata
from datetime import datetime

logger = logging.getLogger(__name__)

# 支持的图片格式
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.tif', '.webp'}

# ===== 可配置命名规则模板 =====
# 占位符: {seq} 花名册序号, {name} 姓名, {idcard} 身份证号（无则为空）
# 同一人员有多个文件时自动追加 (2) (3) ... 序号后缀
NAME_TEMPLATE = '{seq:02d}-{name}'

# 待处理文件夹与报告/日志文件名（生成在输出目录中）
PENDING_DIR_NAME = '待处理'
REPORT_FILENAME = '失败明细报告.xlsx'
LOG_FILENAME = '处理日志.json'

# 业务词与单位词（与历史版本剔除规则保持一致）
BUSINESS_WORDS = r'(劳动合同书|劳动合同|合同书|扫描件|复印件|电子版|合同|协议)'
COMPANY_WORDS = (
    r'(有限责任公司|有限公司|股份公司|机械厂|工厂|门市部|经营部|'
    r'事务所|工作室|服务中心|公司|集团|中心|车间|班组|厂|店|部)'
)


def _normalize_name(name):
    """姓名归一化（用于精确比对）：全角转半角 + 去前后空格（含全角空格）

    NFKC 归一化可将全角字母/数字/空格/标点转为半角等价形式，
    确保文件名与花名册两侧的全角/半角差异不影响比对结果。
    """
    if not name:
        return ''
    return unicodedata.normalize('NFKC', str(name)).strip()


def _clean_raw_name(raw_name):
    """清洗原始名称：去扩展名/PDF分页后缀/业务词/单位词/序号前缀，容错空格与分隔符"""
    # v1.1.42: 先做全角->半角归一化（全角空格/数字/分隔符一并转为半角），
    # 使序号剥离与姓名提取对全角字符同样生效
    name = unicodedata.normalize('NFKC', raw_name.strip())

    # 去掉扩展名
    name = re.sub(r'\.[Pp][Dd][Ff]$', '', name)

    # 去掉PDF分页后缀 (如 xxx.pdf_p1 / xxx_p2)
    name = re.sub(r'_[Pp]\d+$', '', name)

    # 剔除业务词（任意位置）
    name = re.sub(BUSINESS_WORDS, '', name)

    # 剔除单位/机构词（仅当剔除后仍能提取到姓名时才采用，避免误伤含这些字的姓名）
    stripped = re.sub(COMPANY_WORDS, '', name)
    if re.search(r'[\u4e00-\u9fa5]{2,4}', stripped):
        name = stripped

    # 去掉常见业务前缀
    name = re.sub(r'^(扫描件|合同|劳动合同|合同书|协议)[_\-\s]*', '', name)

    # 剥离开头的序号部分 (支持 01、1、001 等数字 + 分隔符 . - _ 空格)
    name = re.sub(r'^\d+[\.\-_\s]*', '', name)

    return name.strip()


def _extract_name_candidates(raw_name):
    """从原始名称中提取全部候选姓名（去重保序）

    容错处理: 空格、点、横线、下划线等分隔符隔开的多段中文各自成为候选；
    连续无法切分的长串按贪心 2-4 字切分。

    Returns:
        list[str]: 候选姓名列表（可能为空）
    """
    cleaned = _clean_raw_name(raw_name)
    if not cleaned:
        return []

    # 连续中文段（2-4字）——分隔符会自然切断
    segments = re.findall(r'[\u4e00-\u9fa5]{2,4}', cleaned)

    seen = set()
    out = []
    for s in segments:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _extract_name(raw_name):
    """兼容接口：提取第一个候选姓名（旧调用方使用）"""
    candidates = _extract_name_candidates(raw_name)
    if candidates:
        return candidates[0]
    return _clean_raw_name(raw_name)


def _build_roster_index(roster):
    """建立花名册索引: 归一化姓名 -> [人员, ...]（同名员工会对应多个人员）

    v1.1.42: 姓名经 _normalize_name 归一化（全角转半角、去前后空格）后作为索引键，
    与文件名提取的候选姓名（同样归一化）比对，忽略前后空格及全角/半角差异。
    """
    index = {}
    for person in roster:
        name = _normalize_name(person.get('name') or '')
        if name:
            index.setdefault(name, []).append(person)
    return index


def _render_new_base(person):
    """按可配置模板渲染新文件名主干（不含扩展名和序号后缀）

    v1.1.42: 姓名经 _normalize_name 归一化（去前后空格/全角转半角），
    避免花名册中带前后空格的姓名直接进入输出文件名。
    """
    name = _normalize_name(person.get('name'))
    try:
        return NAME_TEMPLATE.format(
            seq=person.get('seq', 0),
            name=name,
            idcard=person.get('idcard', '') or '',
        )
    except (KeyError, IndexError, ValueError):
        # 模板占位符异常时回退默认规则
        return '{seq:02d}-{name}'.format(
            seq=person.get('seq', 0),
            name=name,
        )


def _unique_name(output_dir, base, ext):
    """生成不冲突的文件名：base.ext 已存在时追加 (2) (3) ..."""
    candidate = base + ext
    counter = 2
    while os.path.exists(os.path.join(output_dir, candidate)):
        candidate = f'{base}({counter}){ext}'
        counter += 1
    return candidate


def rename_contract_images(file_paths, roster, output_dir, folder_hints=None,
                           resolutions=None, progress_callback=None):
    """按花名册对劳动合同文件批量智能重命名

    命名规则（NAME_TEMPLATE 可配置，默认）:
    - 单人单文件: {seq:02d}-{姓名}.{ext}
    - 单人多文件: {seq:02d}-{姓名}(1).{ext}, {seq:02d}-{姓名}(2).{ext}, ...
      （追加运行时自动衔接输出目录中已有的编号）

    冲突处理:
    - 花名册存在同名员工 -> 冲突项（暂停，等待人工确认）
    - 文件名命中多个花名册姓名 -> 冲突项（暂停，等待人工确认）
    - 冲突文件本次运行不做任何移动/重命名，仅返回冲突明细

    人工确认（resolutions，用于冲突确认后的追加运行）:
    - {原文件名: {'seq': 花名册序号}}  -> 按指定人员重命名
    - {原文件名: {'action': 'pending'}} -> 移入待处理文件夹

    Args:
        file_paths: 文件路径列表
        roster: 花名册人员列表 [{'seq': 1, 'name': '张三', 'idcard': ''}, ...]
        output_dir: 输出目录
        folder_hints: 兼容保留参数（v1.1.42 起姓名匹配仅依据图片文件自身的文件名，
                      文件夹名不再参与匹配，此参数被忽略）
        resolutions: 可选，人工确认结果映射
        progress_callback: 可选，callback(current, total) 处理进度

    Returns:
        dict: {
            'renamed': [{original, new_name, person_seq, person_name}],
            'unmatched': [{original, guessed, reason}],
            'conflicts': [{original, path, guessed, reason, candidates: [{seq, name}]}],
            'total': int,
            'matched_count': int,
            'unmatched_count': int,
            'conflict_count': int,
            'output_dir': str,
        }
    """
    os.makedirs(output_dir, exist_ok=True)

    roster_index = _build_roster_index(roster)
    # 序号 -> 人员（用于人工确认按 seq 指定）
    roster_by_seq = {}
    for person in roster:
        roster_by_seq[person.get('seq')] = person

    resolutions = resolutions or {}

    # ---------- 阶段1: 姓名提取与匹配分类 ----------
    matched_files = {}    # {seq: [(path, basename)]}
    unmatched = []        # [{original, guessed, reason}]
    conflicts = []        # [{original, path, guessed, reason, candidates}]
    file_info = []        # [(path, basename, guessed, source)]

    for fp in file_paths:
        basename = os.path.basename(fp)
        file_base = os.path.splitext(basename)[0]

        # v1.1.42: 姓名仅从图片文件自身的文件名读取，文件夹名不再作为姓名来源；
        # 候选姓名与花名册"姓名"字段（两侧均归一化）为唯一匹配依据
        candidates_names = _extract_name_candidates(file_base)
        source = 'filename'
        guessed = candidates_names[0] if candidates_names else ''

        file_info.append((fp, basename, guessed, source))

        # ---- 人工确认结果优先 ----
        resolution = resolutions.get(basename)
        if resolution:
            if resolution.get('action') == 'pending':
                unmatched.append({
                    'original': basename,
                    'guessed': guessed,
                    'reason': '人工确认：移入待处理文件夹',
                })
                continue
            seq = resolution.get('seq')
            person = roster_by_seq.get(seq)
            if person is None:
                unmatched.append({
                    'original': basename,
                    'guessed': guessed,
                    'reason': f'人工确认的花名册序号 {seq} 不存在',
                })
                continue
            matched_files.setdefault(person['seq'], []).append((fp, basename))
            continue

        # ---- 正常匹配流程 ----
        # 命中的花名册姓名（完全一致才算命中）
        hits = [c for c in candidates_names if c in roster_index]

        if len(hits) >= 2:
            # 文件名中含多个姓名 -> 冲突
            cand_persons = []
            for h in hits:
                for p in roster_index[h]:
                    cand_persons.append({'seq': p.get('seq'), 'name': p.get('name')})
            conflicts.append({
                'original': basename,
                'path': fp,
                'guessed': '、'.join(hits),
                'reason': '文件名中包含多个花名册姓名',
                'candidates': cand_persons,
            })
            logger.warning(f'冲突(多姓名): {basename!r} 命中 {hits}')
        elif len(hits) == 1:
            persons = roster_index[hits[0]]
            if len(persons) > 1:
                # 花名册存在同名员工 -> 冲突
                conflicts.append({
                    'original': basename,
                    'path': fp,
                    'guessed': hits[0],
                    'reason': '花名册中存在同名员工，无法确定归属',
                    'candidates': [{'seq': p.get('seq'), 'name': p.get('name')} for p in persons],
                })
                logger.warning(f'冲突(同名): {basename!r} 花名册有 {len(persons)} 个 {hits[0]!r}')
            else:
                seq = persons[0].get('seq')
                matched_files.setdefault(seq, []).append((fp, basename))
        else:
            # 未命中 —— 给出明确的失败原因
            if candidates_names:
                reason = f'提取姓名"{guessed}"在花名册中查无此人，请核对文件名或补充花名册'
            else:
                reason = '文件名中无法识别出姓名，无法与花名册匹配'
            unmatched.append({'original': basename, 'guessed': guessed, 'reason': reason})
            logger.warning(f'未匹配: {basename!r} 提取={guessed!r} ({reason})')

    # ---------- 阶段2: 按花名册重命名（含编号衔接） ----------
    renamed = []

    # 统计输出目录中每个人员已有的文件数（支持冲突确认后的追加运行编号衔接）
    existing_counts = {}
    if matched_files and os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            for seq in matched_files:
                person = roster_by_seq.get(seq)
                if not person:
                    continue
                base = _render_new_base(person)
                if fname.startswith(base):
                    existing_counts[seq] = existing_counts.get(seq, 0) + 1

    total = len(file_paths)
    done = 0
    for person in roster:
        seq = person.get('seq')
        files = matched_files.get(seq, [])
        if not files:
            continue

        base = _render_new_base(person)
        existing = existing_counts.get(seq, 0)

        for idx, (fp, basename) in enumerate(files):
            ext = os.path.splitext(basename)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                ext = '.jpg'  # 兜底

            n = existing + idx  # 该人员的第 n 个文件（0 起）
            if n == 0:
                new_name = _unique_name(output_dir, base, ext)
            else:
                new_name = _unique_name(output_dir, f'{base}({n + 1})', ext)

            new_path = os.path.join(output_dir, new_name)
            shutil.copy2(fp, new_path)
            renamed.append({
                'original': basename,
                'new_name': new_name,
                'person_seq': seq,
                'person_name': _normalize_name(person.get('name')),
            })

            done += 1
            if progress_callback:
                try:
                    progress_callback(done, total)
                except Exception:
                    pass

    # ---------- 阶段3: 匹配失败文件移入待处理文件夹 ----------
    pending_dir = os.path.join(output_dir, PENDING_DIR_NAME)
    if unmatched:
        os.makedirs(pending_dir, exist_ok=True)
        # 原文件名 -> 路径映射（同名文件取第一个）
        path_by_basename = {}
        for fp, basename, _, _ in file_info:
            path_by_basename.setdefault(basename, fp)
        for item in unmatched:
            fp = path_by_basename.get(item['original'])
            if fp and os.path.exists(fp):
                dest = os.path.join(pending_dir, item['original'])
                counter = 1
                while os.path.exists(dest):
                    stem, ext = os.path.splitext(item['original'])
                    dest = os.path.join(pending_dir, f'{stem}({counter}){ext}')
                    counter += 1
                shutil.copy2(fp, dest)

    result = {
        'renamed': renamed,
        'unmatched': unmatched,
        'conflicts': conflicts,
        'total': total,
        'matched_count': len(renamed),
        'unmatched_count': len(unmatched),
        'conflict_count': len(conflicts),
        'output_dir': output_dir,
    }

    logger.info(
        '文件重命名完成: 共 %d 个文件, 匹配 %d, 待处理 %d, 冲突 %d',
        result['total'], result['matched_count'],
        result['unmatched_count'], result['conflict_count'],
    )

    return result


# ===== 失败明细报告（Excel） =====

def write_failure_report(output_dir, failures):
    """生成失败明细报告（Excel），写入输出目录的"待处理"文件夹

    Args:
        output_dir: 输出目录
        failures: [{original, guessed, reason, candidates?}, ...]
                  candidates: [{'seq', 'name'}] 冲突候选人员（可选）
    """
    if not failures:
        return None

    pending_dir = os.path.join(output_dir, PENDING_DIR_NAME)
    os.makedirs(pending_dir, exist_ok=True)
    report_path = os.path.join(pending_dir, REPORT_FILENAME)

    try:
        import openpyxl
    except ImportError:
        logger.error('生成失败明细报告需要 openpyxl')
        return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '失败明细'

    headers = ['序号', '原文件名', '提取姓名', '失败原因', '候选人员', '处理建议']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)

    for i, item in enumerate(failures, 1):
        candidates = item.get('candidates') or []
        cand_str = '；'.join(f"{c.get('seq')}-{c.get('name')}" for c in candidates)
        if candidates:
            suggestion = '请在系统中人工确认归属后重新处理'
        elif '查无此人' in item.get('reason', '') or '不在花名册' in item.get('reason', ''):
            suggestion = '请核对文件名或补充花名册'
        else:
            suggestion = '请手动重命名后放入结果文件夹'
        ws.append([
            i,
            item.get('original', ''),
            item.get('guessed', ''),
            item.get('reason', ''),
            cand_str,
            suggestion,
        ])

    # 简单列宽
    widths = [6, 36, 12, 30, 24, 32]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

    wb.save(report_path)
    logger.info(f'失败明细报告已生成: {report_path} ({len(failures)} 条)')
    return report_path


# ===== 处理日志（JSON，可回溯） =====

def write_process_log(output_dir, task_id, result):
    """生成处理日志（JSON）：成功与失败明细、原文件名->新文件名映射

    Args:
        output_dir: 输出目录
        task_id: 任务ID
        result: 聚合结果 dict（renamed / unmatched / conflicts / 统计字段）
    """
    log_path = os.path.join(output_dir, LOG_FILENAME)

    now = datetime.now().isoformat(timespec='seconds')
    log = {
        'task_id': task_id,
        'generated_at': now,
        'naming_template': NAME_TEMPLATE,
        'summary': {
            'total': result.get('total', 0),
            'matched': result.get('matched_count', 0),
            'pending': result.get('unmatched_count', 0),
            'conflicts_resolved': result.get('conflicts_resolved_count', 0),
        },
        'successes': [
            {
                'time': now,
                'original': item['original'],
                'new_name': item['new_name'],
                'person_seq': item.get('person_seq'),
                'person_name': item.get('person_name'),
            }
            for item in result.get('renamed', [])
        ],
        'failures': [
            {
                'time': now,
                'original': item['original'],
                'guessed': item.get('guessed', ''),
                'reason': item.get('reason', ''),
            }
            for item in result.get('unmatched', [])
        ],
    }

    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        logger.info(f'处理日志已生成: {log_path}')
    except OSError as e:
        logger.error(f'处理日志写入失败: {e}')

    return log_path
