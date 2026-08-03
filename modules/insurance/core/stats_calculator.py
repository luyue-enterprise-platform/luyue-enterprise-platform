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


def calc_overlap(insurances):
    """
    计算四险重叠时间段

    Args:
        insurances: dict {险种: (start_ym, end_ym)}

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

    if ym_to_int(overlap_start) <= ym_to_int(overlap_end):
        months = months_between(overlap_start, overlap_end)
        return {'overlap_start': overlap_start, 'overlap_end': overlap_end,
                'overlap_months': months, 'has_overlap': True}
    else:
        return {'overlap_start': None, 'overlap_end': None,
                'overlap_months': 0, 'has_overlap': False}


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


def get_overlap_years(persons_overlaps):
    """
    从所有人的重叠结果中，确定需要显示的年度列

    Args:
        persons_overlaps: list of calc_overlap()返回的dict

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

    return sorted(all_years)


def calc_person_stats(person, year_range=None):
    """
    计算单个人的完整统计信息

    Args:
        person: dict {'name', 'idcard', 'insurances': {险种: (start, end)}}
        year_range: 可选，('YYYY-MM', 'YYYY-MM') 元组（起始年月, 截止年月）
                    当提供时，重叠时间段将裁剪到该年月范围，
                    年度列使用用户选择的范围而非自动检测

    Returns:
        dict: 完整统计信息
    """
    overlap = calc_overlap(person['insurances'])

    # 如果指定了年月范围，裁剪重叠时间段到精确月份
    if year_range and overlap['has_overlap']:
        period_start, period_end = year_range  # ('2022-03', '2025-06')
        # 裁剪起始月：如果重叠起始早于用户选择的起始月，用用户起始月
        if ym_to_int(overlap['overlap_start']) < ym_to_int(period_start):
            overlap['overlap_start'] = period_start
        # 裁剪截止月：如果重叠截止晚于用户选择的截止月，用用户截止月
        if ym_to_int(overlap['overlap_end']) > ym_to_int(period_end):
            overlap['overlap_end'] = period_end

        # 裁剪后重新检查有效性
        if ym_to_int(overlap['overlap_start']) > ym_to_int(overlap['overlap_end']):
            overlap['has_overlap'] = False
            overlap['overlap_start'] = None
            overlap['overlap_end'] = None
            overlap['overlap_months'] = 0
        else:
            overlap['overlap_months'] = months_between(
                overlap['overlap_start'], overlap['overlap_end']
            )

    # 确定年度列
    if year_range:
        # 从年月范围推导年度列（如 '2022-03' ~ '2025-06' → [2022, 2023, 2024, 2025]）
        sy, _ = parse_ym(year_range[0])
        ey, _ = parse_ym(year_range[1])
        years = list(range(sy, ey + 1))
    else:
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
        year_range: 可选，('YYYY-MM', 'YYYY-MM') 元组（起始年月, 截止年月）
                    当提供时，统计基于用户选择的年月范围

    Returns:
        tuple: (list of person_stats, list of year_columns)
    """
    person_stats = [calc_person_stats(p, year_range) for p in persons]

    if year_range:
        # 从年月范围推导年度列
        sy, _ = parse_ym(year_range[0])
        ey, _ = parse_ym(year_range[1])
        all_years = list(range(sy, ey + 1))
    else:
        # 自动检测：从所有有重叠的人员中收集年份
        all_years = get_overlap_years(
            [ps for ps in person_stats if ps['has_overlap']]
        )

    return person_stats, all_years
