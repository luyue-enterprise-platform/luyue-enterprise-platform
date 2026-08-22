# -*- coding: utf-8 -*-
"""统计计算模块 - 合并参保时间段，计算统计时间段、总月数、每年参保月数

v1.1.36 统计规则：
1. 重叠合并：多个参保时间段存在重叠时合并为一段，合并后区间按最早开始时间
   与最晚结束时间（最后截止时间）确定，重叠部分不重复计算
2. 范围限制：每年月数严格只统计用户设置起止范围内的月份，超出范围不计入
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
    计算统计时间段（重叠时间段）—— v1.1.36 合并规则

    规则：
    1. 收集该人员所有险种的参保时间段（每个险种上游已归并为最近连续一段）
    2. 存在重叠的时间段合并为一段：合并后区间按最早开始时间与最晚结束时间
       （最后截止时间）确定，重叠部分不重复计算
    3. 完全不重叠的时间段不合并；统计取"存在重叠"的合并段中
       最后截止时间最晚的一段
    4. 统计结果与用户设置的年月范围取交集，超出范围的时间不计入

    Args:
        insurances: dict {险种: (start_ym, end_ym)}
        year_range: 可选, (start_ym, end_ym) 元组，用户设置的统计起止范围

    Returns:
        dict: {
            'overlap_start': 统计起始,
            'overlap_end': 统计截止（最后截止时间，超出用户范围时截断）,
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

    # 合并重叠/相邻时间段，只保留段内存在真实重叠（>=2 个原始时间段）的合并段
    clusters = merge_periods(periods)
    overlapped = [c for c in clusters if c[2] >= 2]
    if not overlapped:
        return no_result

    # 取最后截止时间最晚的一段；区间 = 该段最早开始 ~ 最晚结束
    overlap_start, overlap_end, _ = max(overlapped, key=lambda c: ym_to_int(c[1]))

    # 与用户选择的年月范围取交集（超出范围的时间不计入统计）
    if year_range:
        try:
            rs, re_ = year_range
            if ym_to_int(overlap_start) < ym_to_int(rs):
                overlap_start = rs
            if ym_to_int(overlap_end) > ym_to_int(re_):
                overlap_end = re_
            if ym_to_int(overlap_start) > ym_to_int(overlap_end):
                return no_result
        except (ValueError, TypeError, AttributeError):
            pass

    months = months_between(overlap_start, overlap_end)
    return {'overlap_start': overlap_start, 'overlap_end': overlap_end,
            'overlap_months': months, 'has_overlap': True}


def year_overlap_months(overlap_start, overlap_end, year):
    """
    计算某一年在重叠时间段内的参保月数

    Args:
        overlap_start: 'YYYY-MM'
        overlap_end: 'YYYY-MM'
        year: 年份整数

    Returns:
        int: 该年的重叠月数
    """
    if not overlap_start or not overlap_end:
        return 0

    sy, sm = parse_ym(overlap_start)
    ey, em = parse_ym(overlap_end)

    if year < sy or year > ey:
        return 0
    if sy == ey == year:
        return em - sm + 1
    if year == sy:
        return 12 - sm + 1
    if year == ey:
        return em
    return 12


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
    计算单个人的完整统计信息

    Args:
        person: dict {'name', 'idcard', 'insurances': {险种: (start, end)}}
        year_range: 可选, (start_ym, end_ym) 元组，限制重叠的时间范围

    Returns:
        dict: 完整统计信息
    """
    overlap = calc_overlap(person['insurances'], year_range=year_range)

    # 确定年度列
    years = []
    if overlap['has_overlap']:
        sy, _ = parse_ym(overlap['overlap_start'])
        ey, _ = parse_ym(overlap['overlap_end'])
        years = list(range(sy, ey + 1))

    # 每年重叠月数
    yearly_months = {}
    for y in years:
        yearly_months[y] = year_overlap_months(
            overlap['overlap_start'], overlap['overlap_end'], y
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
