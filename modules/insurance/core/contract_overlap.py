# -*- coding: utf-8 -*-
"""劳动合同起止时间与四险参保时间段叠加比对（v1.1.53 新增）

业务规则（用户确认版）：
1. 花名册新增"劳动合同起止时间"列，解析出合同时间段（同一员工可多份合同，
   段间用 ；;、，, 或换行 分隔）
2. 月粒度换算：合同开始/结束日**所在月份**即视为合同覆盖月（哪怕只覆盖部分天数）
   —— 与参保月粒度对齐，保证"其余统计规则不变"
3. 无固定期限 / 至今 / 长期 → 结束月视为开放（不裁剪参保期后端）
4. 有效参保期 = 四险重叠参保时间段 ∩ 合同时间段（两两求交后合并重叠/相邻段）
   —— 数学上等价于"各险种先裁剪再求四险交集"，但保持 persons['insurances']
   原始值不变，仅作用于 calc_all_stats 产出的重叠结果层
5. 合同列为空 / 格式异常 → **不裁剪**，仅生成标注提示（用户选定策略：缺失=信息不全，
   不静默清零；补录后重跑即可生效）
6. 其余统计规则、计算流程、输出格式均保持不变——本模块只对 calc_all_stats
   的结果做后处理，不改 stats_calculator 的任何函数
"""
import re
from datetime import date, datetime

from modules.insurance.core.stats_calculator import (
    ym_to_int, parse_ym, year_overlap_months,
)

OPEN_END = '9999-12'  # 无固定期限合同的开放结束月（内部哨兵值）

# 合同段分隔符：分号、顿号、逗号、换行
_SEG_SPLIT = re.compile(r'[；;、，,\n]+')

# 日期主正则（交替顺序即优先级：先日级、后月级，先中文、后符号、最后纯数字）
_DATE_RE = re.compile(
    r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?'   # 2023年1月5日 / 2023年1月5
    r'|(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})'               # 2023-01-05 / 2023/1/5 / 2023.1.5
    r'|(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)'                # 20230105
    r'|(\d{4})\s*年\s*(\d{1,2})\s*月'                     # 2023年1月
    r'|(\d{4})[-/.](\d{1,2})'                             # 2023-01 / 2023/1 / 2023.1
    r'|(?<!\d)(\d{4})(\d{2})(?!\d)'                       # 202301
)

# 开放结束关键字（无固定期限合同）
_OPEN_END_RE = re.compile(r'至今|迄今|现在|长期|无固定期限|不限期|open', re.IGNORECASE)

# 年份合理范围（防 OCR/录入垃圾）
_MIN_YEAR, _MAX_YEAR = 1990, 2100


def _int_to_ym(v):
    """ym_to_int 的逆运算：整数月 → 'YYYY-MM'"""
    y, m = divmod(v, 12)
    if m == 0:
        y, m = y - 1, 12
    return f'{y:04d}-{m:02d}'


def _valid_ym(y, m):
    return _MIN_YEAR <= y <= _MAX_YEAR and 1 <= m <= 12


def _extract_dates(text):
    """从一段文本中提取全部日期，返回 [(ym_str, has_day), ...]（按出现顺序）"""
    out = []
    for m in _DATE_RE.finditer(text):
        g = m.groups()
        if g[0] is not None:        # 中文年月日
            y, mo, d, has_day = int(g[0]), int(g[1]), int(g[2]), True
        elif g[3] is not None:      # YYYY-MM-DD 等
            y, mo, d, has_day = int(g[3]), int(g[4]), int(g[5]), True
        elif g[6] is not None:      # YYYYMMDD
            y, mo, d, has_day = int(g[6]), int(g[7]), int(g[8]), True
        elif g[9] is not None:      # 中文年月
            y, mo, d, has_day = int(g[9]), int(g[10]), 1, False
        elif g[11] is not None:     # YYYY-MM 等
            y, mo, d, has_day = int(g[11]), int(g[12]), 1, False
        else:                       # YYYYMM
            y, mo, d, has_day = int(g[13]), int(g[14]), 1, False
        if not _valid_ym(y, mo):
            raise ValueError(f'日期超出合理范围: {y}-{mo:02d}')
        if has_day:
            try:
                date(y, mo, d)      # 校验真实日期（如 2023-02-30 非法）
            except ValueError:
                raise ValueError(f'非法日期: {y}-{mo:02d}-{d:02d}')
        out.append(f'{y:04d}-{mo:02d}')
    return out


def parse_contract_cell(text):
    """解析花名册"劳动合同起止时间"单元格

    Returns:
        dict: {
            'status': 'ok' | 'missing' | 'invalid',
            'periods': [(start_ym, end_ym), ...]   # 月粒度，开放结束为 OPEN_END
            'error': 错误说明（invalid 时）,
            'raw': 原始文本,
        }
    """
    raw = '' if text is None else str(text).strip()
    if not raw or raw.lower() in ('none', 'nan', '-', '—', '/', '无'):
        return {'status': 'missing', 'periods': [], 'error': '', 'raw': raw}

    periods = []
    pieces = [p.strip() for p in _SEG_SPLIT.split(raw) if p and p.strip()]
    try:
        for piece in pieces:
            dates = _extract_dates(piece)
            if not dates:
                raise ValueError(f'未识别到日期: 「{piece[:20]}」')
            if len(dates) == 1:
                # 单日期 + 开放结束关键字 → 起始日 + 无固定期限
                if _OPEN_END_RE.search(piece):
                    periods.append((dates[0], OPEN_END))
                    continue
                raise ValueError(f'起止需成对（仅识别到 {dates[0]}）: 「{piece[:20]}」')
            if len(dates) > 2:
                raise ValueError(f'一段内含 3 个以上日期: 「{piece[:24]}」')
            start_ym, end_ym = dates
            # 结束位置之后出现开放关键字（如 "2023-01-01 至 2025-12-31 后续签无固定期限"
            # 已被段分隔符拆开，这里只处理 "2023-01-01 ~ 至今" 已由单日期分支覆盖的情况）
            if ym_to_int(start_ym) > ym_to_int(end_ym):
                raise ValueError(f'开始晚于结束: {start_ym} ~ {end_ym}')
            periods.append((start_ym, end_ym))
    except ValueError as e:
        return {'status': 'invalid', 'periods': [], 'error': str(e), 'raw': raw}

    # 合同段按开始月排序 + 合并重叠/相邻段（同一员工多份合同视为连续覆盖）
    periods.sort(key=lambda p: ym_to_int(p[0]))
    merged = [list(periods[0])]
    for s, e in periods[1:]:
        if ym_to_int(s) <= ym_to_int(merged[-1][1]) + 1:
            if ym_to_int(e) > ym_to_int(merged[-1][1]):
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return {'status': 'ok', 'periods': [tuple(p) for p in merged], 'error': '', 'raw': raw}


def intersect_periods(base_start, base_end, segments):
    """基础时间段与合同多段两两求交，合并重叠/相邻段（月粒度）

    Args:
        base_start/base_end: 'YYYY-MM' 基础参保重叠段
        segments: [(start_ym, end_ym), ...] 合同段（可为 OPEN_END）

    Returns:
        list of (start_ym, end_ym): 有效参保期分段（升序，互不重叠/相邻）；
        无重叠返回 []
    """
    bs, be = ym_to_int(base_start), ym_to_int(base_end)
    pieces = []
    for s, e in segments or []:
        lo, hi = max(bs, ym_to_int(s)), min(be, ym_to_int(e))
        if lo <= hi:
            pieces.append((lo, hi))
    if not pieces:
        return []
    pieces.sort()
    merged = [list(pieces[0])]
    for lo, hi in pieces[1:]:
        if lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(_int_to_ym(a), _int_to_ym(b)) for a, b in merged]


def _note(ps, msg, old=''):
    """生成一条合同比对提示（结构与手动操作记录一致，展示在操作记录面板）"""
    return {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': '合同比对',
        'name': ps.get('name', ''),
        'idcard': ps.get('idcard', ''),
        'insurance_type': '—',
        'old': old,
        'new': msg,
    }


def apply_contract_to_stats(person_stats, roster, year_range=None):
    """对 calc_all_stats 的结果执行合同叠加裁剪（原地修改 person_stats）

    - 仅裁剪 person_stats 的重叠结果层（overlap_*/yearly_months/years），
      不触碰 insurances 各险种原始时间段
    - 合同缺失/格式异常/人员不在花名册 → 不裁剪；缺失与异常生成标注提示
    - 有效参保期可能为多段（合同有间断）：overlap_start/end 取首末段包络，
      月数与各年月数按实际分段求和（间断月不计入）

    Args:
        person_stats: calc_all_stats 返回的人员统计列表
        roster: 花名册（含 contract_periods/contract_status，由 roster_parser 解析）
        year_range: 可选用户筛选范围（与 v1.1.38 规则一致，年度/总月数跟筛选走）

    Returns:
        list: 合同比对提示条目（未持久化，每次重建重新生成）
    """
    from modules.insurance.core.roster_parser import match_record_to_roster

    notes = []
    if not roster:
        return notes

    for ps in person_stats:
        entry = match_record_to_roster(
            {'name': ps.get('name', ''), 'idcard': ps.get('idcard', '')}, roster)
        if not entry:
            continue  # 不在花名册 → 无合同信息，不裁剪不标注（花名册补全人员才有合同列）
        status = entry.get('contract_status', 'missing')
        if status == 'invalid':
            notes.append(_note(ps, f'劳动合同起止时间格式异常（{entry.get("contract_error", "")}），'
                                   f'本次按未裁剪结果统计，请修正后重跑'))
            continue
        periods = entry.get('contract_periods') or []
        if status != 'ok' or not periods:
            notes.append(_note(ps, '劳动合同起止时间未登记，本次按未裁剪结果统计，请补录后重跑'))
            continue
        if not ps.get('has_overlap'):
            continue  # 四险本身无重叠，无需比对

        old_range = f"{ps['overlap_start']}~{ps['overlap_end']}"
        eff = intersect_periods(ps['overlap_start'], ps['overlap_end'], periods)
        if not eff:
            ps['has_overlap'] = False
            ps['overlap_start'] = None
            ps['overlap_end'] = None
            ps['overlap_months'] = 0
            ps['years'] = []
            ps['yearly_months'] = {}
            notes.append(_note(ps, '合同期与参保期无重叠，有效参保期为空', old=old_range))
            continue

        new_start, new_end = eff[0][0], eff[-1][1]
        years = list(range(parse_ym(new_start)[0], parse_ym(new_end)[0] + 1))
        yearly = {
            y: sum(year_overlap_months(s, e, y, year_range=year_range) for s, e in eff)
            for y in years
        }
        ps['overlap_start'] = new_start
        ps['overlap_end'] = new_end
        ps['years'] = years
        ps['yearly_months'] = yearly
        ps['overlap_months'] = sum(yearly.values())  # 与 v1.1.38 一致：总月数跟筛选走

        if len(eff) > 1:
            seg_text = '、'.join(f'{s}~{e}' for s, e in eff)
            notes.append(_note(ps, f'合同分段叠加，有效参保期合并为 {seg_text}', old=old_range))
        elif (new_start, new_end) != tuple(old_range.split('~')):
            notes.append(_note(ps, f'有效参保期 {new_start}~{new_end}', old=old_range))
        # 合同完全覆盖参保期（未发生裁剪）→ 不记提示

    return notes
