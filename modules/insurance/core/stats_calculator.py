# -*- coding: utf-8 -*-
"""统计计算模块 - 合并参保时间段，计算统计时间段、总月数、每年参保月数

v1.1.37 统计规则：
1. 重叠合并：多个参保时间段存在重叠时合并为一段，合并后区间按最早开始时间
   与最晚结束时间（最后截止时间）确定，重叠部分不重复计算
2. **重叠时间段固定**：四险全量数据的合并重叠区间，不受用户筛选 year_range
   影响——无论用户选任何范围，重叠区间始终是真实数据反映
3. **年度月数跟筛选走**：每年月数严格只统计用户设置起止范围内的月份，
   超出范围则该年度月数=0
"""

# 四险标准顺序
INSURANCE_ORDER = ['养老保险', '医疗保险', '工伤保险', '失业保险']


def parse_ym(ym_str):
    """将 'YYYY-MM' 字符串解析为 (年, 月) 整数元组"""
    y, m = map(int, ym_str.split('-'))
    return y, m


def ym_to_int(ym_str):
    """将 'YYYY-MM' 转为可比月的整数（年*12+月）"""
    y, m = parse_ym(ym_str)
    return y * 12 + m


def months_between(start, end):
    """计算两个年月之间的月数（含首尾）"""
    return ym_to_int(end) - ym_to_int(start) + 1


def merge_periods(periods):
    """
    合并重叠/相邻的参保时间段（区间并集）

    合并规则（每一段合并后区间 = 该段内最早开始时间 ~ 最晚结束时间，
    重叠月份只计算一次，不重复累计）：
    - 部分重叠：[2023-01,2023-12] 与 [2023-06,2024-05] → [2023-01,2024-05]
    - 完全包含：[2022-01,2024-12] 包含 [2023-01,2023-06] → [2022-01,2024-12]
    - 首尾相邻：[2023-01,2023-12] 与 [2024-01,2024-06] → [2023-01,2024-06]
    - 跨年重叠：按合并后的整段处理，年度月数由 year_overlap_months 分年切分
    - 完全不重叠的时间段保持独立，不跨空档强行合并

    Args:
        periods: list of (start_ym, end_ym)，元素为 'YYYY-MM' 字符串元组

    Returns:
        list of (start_ym, end_ym, n_periods)：按开始时间升序的合并段，
        n_periods 为该段内包含的原始时间段个数（>=2 表示段内存在真实重叠/相邻）
    """
    valid = [p for p in periods if p and len(p) >= 2 and p[0] and p[1]]
    if not valid:
        return []

    sorted_periods = sorted(valid, key=lambda p: ym_to_int(p[0]))

    merged = []
    cur_start, cur_end = sorted_periods[0][0], sorted_periods[0][1]
    cur_count = 1

    for s, e in sorted_periods[1:]:
        if ym_to_int(s) <= ym_to_int(cur_end) + 1:
            # 重叠或相邻 → 并入当前段，截止时间取最晚
            if ym_to_int(e) > ym_to_int(cur_end):
                cur_end = e
            cur_count += 1
        else:
            merged.append((cur_start, cur_end, cur_count))
            cur_start, cur_end, cur_count = s, e, 1
    merged.append((cur_start, cur_end, cur_count))

    return merged


def calc_overlap(insurances, year_range=None):
    """
    计算统计时间段（重叠时间段）—— v1.1.37 规则

    规则：
    1. **交集语义**：四险全量数据中，所有险种同时参保的时间段
       = max(各险种起始月) ~ min(各险种截止月)
    2. 重叠区间不受 year_range 影响——重叠起始/截止/月数
       始终是四险全量数据计算结果。year_range 参数保留仅为向后兼容，
       不再影响 overlap_start/end。年度月数截断由 year_overlap_months
       单独处理。
    3. 需要至少 2 个险种有数据才计算重叠（1 个险种无"重叠"概念）
    4. 若 max_start > min_end → 无重叠

    Args:
        insurances: dict {险种: (start_ym, end_ym)}，每个险种 1 段
        year_range: 兼容参数，不再用于裁剪重叠区间

    Returns:
        dict: {
            'overlap_start': 重叠起始（固定为全量数据交集结果）,
            'overlap_end': 重叠截止（固定为全量数据交集结果）,
            'overlap_months': 统计月数（重叠部分只计一次）,
            'has_overlap': 是否有统计结果
        }
    """
    no_result = {'overlap_start': None, 'overlap_end': None,
                 'overlap_months': 0, 'has_overlap': False}

    periods = [v for v in (insurances or {}).values()
               if v and len(v) >= 2 and v[0] and v[1]]
    if len(periods) < 2:
        return no_result

    # v1.1.37: 交集语义——所有险种同时参保的时段
    # = 最晚起始月 ~ 最早截止月
    overlap_start = max(periods, key=lambda p: ym_to_int(p[0]))[0]
    overlap_end = min(periods, key=lambda p: ym_to_int(p[1]))[1]

    if ym_to_int(overlap_start) > ym_to_int(overlap_end):
        return no_result

    months = months_between(overlap_start, overlap_end)
    return {'overlap_start': overlap_start, 'overlap_end': overlap_end,
            'overlap_months': months, 'has_overlap': True}


def year_overlap_months(overlap_start, overlap_end, year, year_range=None):
    """
    计算某一年在重叠时间段内的参保月数 —— v1.1.37 规则

    裁剪优先级：年内 overlap 起止月 → 与 year_range 取交集
    - 年完全在 year_range 外 → 返回 0
    - 年内起月：取 max(年内 overlap 起点, year_range 内该年起月)
    - 年内止月：取 min(年内 overlap 终点, year_range 内该年止月)
    - 起月 > 止月 → 返回 0

    Args:
        overlap_start: 'YYYY-MM'
        overlap_end: 'YYYY-MM'
        year: 年份整数
        year_range: 可选, (start_ym, end_ym) 元组，用户设置的统计起止范围

    Returns:
        int: 该年的重叠月数（已应用 year_range 裁剪）
    """
    if not overlap_start or not overlap_end:
        return 0

    sy, sm = parse_ym(overlap_start)
    ey, em = parse_ym(overlap_end)

    # 年在 overlap 区间外
    if year < sy or year > ey:
        return 0

    # 年内 overlap 边界
    cur_sy, cur_sm = (sy, sm) if year == sy else (year, 1)
    cur_ey, cur_em = (ey, em) if year == ey else (year, 12)

    # 与 year_range 取交集（v1.1.37：年度月数随用户筛选动态变化）
    if year_range:
        try:
            rs, re_ = year_range
            rs_y, rs_m = parse_ym(rs)
            re_y, re_m = parse_ym(re_)
            # year 在 year_range 外 → 0
            if year < rs_y or year > re_y:
                return 0
            # 年内起点：与 year_range 起点取 max
            if year == rs_y and rs_m > cur_sm:
                cur_sm = rs_m
            # 年内终点：与 year_range 终点取 min
            if year == re_y and re_m < cur_em:
                cur_em = re_m
        except (ValueError, TypeError, AttributeError):
            pass

    # 起月 > 止月 → 0
    cur_s_int = cur_sy * 12 + cur_sm
    cur_e_int = cur_ey * 12 + cur_em
    if cur_s_int > cur_e_int:
        return 0
    return cur_e_int - cur_s_int + 1


def get_overlap_years(persons_overlaps, year_range=None):
    """
    从所有人的重叠结果中，确定需要显示的年度列

    Args:
        persons_overlaps: list of calc_overlap()返回的dict
        year_range: 可选, (start_ym, end_ym) 元组，限制年度列范围

    Returns:
        list of int: 需要显示的年份列表（升序）
    """
    all_years = set()
    for ov in persons_overlaps:
        if ov['has_overlap']:
            sy, _ = parse_ym(ov['overlap_start'])
            ey, _ = parse_ym(ov['overlap_end'])
            for y in range(sy, ey + 1):
                all_years.add(y)

    # 若指定了用户选择的年月范围，与重叠年份取交集
    if year_range:
        try:
            start_ym, end_ym = year_range
            rs_y, _ = parse_ym(start_ym)
            re_y, _ = parse_ym(end_ym)
            all_years = {y for y in all_years if rs_y <= y <= re_y}
        except (ValueError, TypeError, AttributeError):
            pass

    return sorted(all_years)


def calc_person_stats(person, year_range=None):
    """
    计算单个人的完整统计信息 —— v1.1.37 规则

    重叠区间不受 year_range 影响；yearly_months 受 year_range 裁剪。

    Args:
        person: dict {'name', 'idcard', 'insurances': {险种: (start, end)}}
        year_range: 可选, (start_ym, end_ym) 元组，用于年度月数裁剪

    Returns:
        dict: 完整统计信息
    """
    # v1.1.37: 不再传 year_range 给 calc_overlap，重叠区间固定为全量数据
    overlap = calc_overlap(person['insurances'])

    # 年度列由 overlap 全量区间决定；year_range 在 yearly_months 内逐月裁剪
    years = []
    if overlap['has_overlap']:
        sy, _ = parse_ym(overlap['overlap_start'])
        ey, _ = parse_ym(overlap['overlap_end'])
        years = list(range(sy, ey + 1))

    # 每年重叠月数（带 year_range 裁剪）
    yearly_months = {}
    for y in years:
        yearly_months[y] = year_overlap_months(
            overlap['overlap_start'], overlap['overlap_end'], y,
            year_range=year_range,
        )

    return {
        'name': person['name'],
        'idcard': person['idcard'],
        'insurances': person['insurances'],
        'overlap_start': overlap['overlap_start'],
        'overlap_end': overlap['overlap_end'],
        'overlap_months': overlap['overlap_months'],
        'has_overlap': overlap['has_overlap'],
        'years': years,
        'yearly_months': yearly_months,
    }


def calc_all_stats(persons, year_range=None):
    """
    计算所有人的统计信息

    Args:
        persons: list of person dict
        year_range: 可选, (start_ym, end_ym) 元组，限制年度列范围

    Returns:
        tuple: (list of person_stats, list of year_columns)
    """
    person_stats = [calc_person_stats(p, year_range=year_range) for p in persons]
    all_years = get_overlap_years(
        [ps for ps in person_stats if ps['has_overlap']],
        year_range=year_range
    )
    return person_stats, all_years
