# -*- coding: utf-8 -*-
"""统计计算模块 - 计算四险重叠时间段、重叠月数、每年参保月数"""

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


def calc_overlap(insurances, year_range=None):
    """
    计算四险重叠时间段（与用户选择的年月范围取交集）

    Args:
        insurances: dict {险种: (start_ym, end_ym)}
        year_range: 可选, (start_ym, end_ym) 元组，限制重叠的时间范围
                   重叠 = 四险重叠 ∩ year_range

    Returns:
        dict: {
            'overlap_start': 重叠起始,
            'overlap_end': 重叠截止,
            'overlap_months': 重叠月数,
            'has_overlap': 是否有重叠
        }
    """
    if not insurances or len(insurances) < 2:
        return {'overlap_start': None, 'overlap_end': None,
                'overlap_months': 0, 'has_overlap': False}

    # 只计算存在的险种的重叠
    starts = [v[0] for v in insurances.values()]
    ends = [v[1] for v in insurances.values()]

    overlap_start = max(starts, key=ym_to_int)
    overlap_end = min(ends, key=ym_to_int)

    if ym_to_int(overlap_start) > ym_to_int(overlap_end):
        return {'overlap_start': None, 'overlap_end': None,
                'overlap_months': 0, 'has_overlap': False}

    # 与用户选择的年月范围取交集
    if year_range:
        try:
            rs, re_ = year_range
            if ym_to_int(overlap_start) < ym_to_int(rs):
                overlap_start = rs
            if ym_to_int(overlap_end) > ym_to_int(re_):
                overlap_end = re_
            if ym_to_int(overlap_start) > ym_to_int(overlap_end):
                return {'overlap_start': None, 'overlap_end': None,
                        'overlap_months': 0, 'has_overlap': False}
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
