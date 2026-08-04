# -*- coding: utf-8 -*-
"""
章节定义与文件匹配引擎
基于样本PDF分析，定义退税/抵税两种模式的章节结构，并从用户文件夹中自动匹配对应文件
"""
import os
import re
import logging

logger = logging.getLogger('pdfmerge.sections')

# ============================================================
# 章节结构定义
# 每个章节: id, name(显示名), keywords(文件名匹配关键词),
#           priority(匹配优先级,数字越小越先匹配), multi(是否多文件),
#           sort_by_roster(是否按花名册排序), required(是否必须)
# ============================================================

# 退税模式章节
SECTIONS_REFUND = [
    {
        'id': 'refund_application',
        'name': '退税申请',
        'keywords': ['退税申请', '退税', '申请退税'],
        'exclude_keywords': ['抵税', '台账', '申报表'],
        'priority': 1,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'general_ledger',
        'name': '总台账',
        'keywords': ['总台账'],
        'exclude_keywords': [],
        'priority': 1,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'employment_cert',
        'name': '企业吸纳重点群体就业认定证明',
        'keywords': ['认定证明', '就业认定', '吸纳'],
        'exclude_keywords': [],
        'priority': 2,
        'multi': False,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'poverty_cert',
        'name': '脱贫人口身份证明',
        'keywords': ['脱贫', '建档立卡', '防止返贫'],
        'exclude_keywords': ['退役', '士兵'],
        'priority': 2,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'veteran_cert',
        'name': '退役士兵身份证明',
        'keywords': ['退役', '士官', '义务兵', '退出现役', '退伍'],
        'exclude_keywords': ['养老', '医疗', '工伤', '失业'],
        'priority': 2,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'original_tax_return',
        'name': '原增值税纳税申报表',
        'keywords': ['原增值税', '原申报', '原始申报'],
        'exclude_keywords': ['修改', '台账', '完税'],
        'priority': 3,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'modified_tax_return',
        'name': '修改后增值税纳税申报表',
        'keywords': ['修改后', '修改增值税', '更正申报', '减免税申报明细'],
        'exclude_keywords': ['台账', '完税'],
        'priority': 3,
        'multi': True,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'annual_ledger',
        'name': '年度台账',
        'keywords': ['台账'],
        'exclude_keywords': ['总台账'],
        'priority': 4,
        'multi': True,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'work_time_table',
        'name': '工作时间表/就业信息表',
        'keywords': ['工作时间', '就业信息', '工作时间表'],
        'exclude_keywords': ['台账', '申报表', '完税'],
        'priority': 4,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'tax_payment_cert',
        'name': '完税证明',
        'keywords': ['完税', '税收完税', '税收通用完税'],
        'exclude_keywords': ['申报表', '台账'],
        'priority': 4,
        'multi': True,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'labor_contract',
        'name': '劳动合同',
        'keywords': ['劳动', '合同', '劳工'],
        'exclude_keywords': ['养老', '医疗', '工伤', '失业', '保险', '参保'],
        'priority': 5,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'pension_insurance',
        'name': '养老保险参保证明',
        'keywords': ['养老', '养老保险'],
        'exclude_keywords': ['工伤', '失业', '医疗', '合同'],
        'priority': 6,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'work_injury_insurance',
        'name': '工伤保险参保证明',
        'keywords': ['工伤', '工伤保险'],
        'exclude_keywords': ['养老', '失业', '医疗', '合同'],
        'priority': 6,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'unemployment_insurance',
        'name': '失业保险参保证明',
        'keywords': ['失业', '失业保险'],
        'exclude_keywords': ['养老', '工伤', '医疗', '合同'],
        'priority': 6,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'medical_insurance',
        'name': '医疗保险参保证明',
        'keywords': ['医疗', '医疗保险', '基本医疗'],
        'exclude_keywords': ['养老', '工伤', '失业', '合同'],
        'priority': 6,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
]

# 抵税模式章节 (基于样本PDF分析：66页，12个章节)
# 样本文件：宝鸡宝运集团所属期2026年3月重点群体抵税备查资料.pdf
# 结构：封面→目录→情况说明→减免税申报明细表→总台账→就业信息表→认定证明→
#       脱贫人口证明→退役士兵证明→劳动合同→养老→工伤→失业→医疗
SECTIONS_DEDUCTION = [
    {
        'id': 'situation_statement',
        'name': '情况说明',
        'keywords': ['情况说明', '享受', '抵减', '政策说明'],
        'exclude_keywords': ['退税', '台账', '申报表', '参保', '合同'],
        'priority': 1,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'tax_return',
        'name': '增值税减免税申报明细表',
        'keywords': ['减免税申报明细', '减免明细', '申报明细', '增值税减免'],
        'exclude_keywords': ['台账', '完税', '总台账'],
        'priority': 1,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'general_ledger',
        'name': '总台账',
        'keywords': ['总台账'],
        'exclude_keywords': [],
        'priority': 2,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'work_time_table',
        'name': '重点群体或自主就业退役士兵就业信息表',
        'keywords': ['就业信息', '就业信息表', '工作时间'],
        'exclude_keywords': ['台账', '申报表', '完税', '参保'],
        'priority': 2,
        'multi': False,
        'sort_by_roster': False,
        'required': True,
    },
    {
        'id': 'employment_cert',
        'name': '企业吸纳重点群体就业认定证明',
        'keywords': ['认定证明', '就业认定', '吸纳'],
        'exclude_keywords': [],
        'priority': 3,
        'multi': False,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'poverty_cert',
        'name': '脱贫人口身份证明',
        'keywords': ['脱贫', '建档立卡', '防止返贫', '返贫监测'],
        'exclude_keywords': ['退役', '士兵', '退出现役'],
        'priority': 3,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'veteran_cert',
        'name': '退役士兵身份证明',
        'keywords': ['退役', '士官', '义务兵', '退出现役', '退伍'],
        'exclude_keywords': ['养老', '医疗', '工伤', '失业', '参保'],
        'priority': 3,
        'multi': True,
        'sort_by_roster': False,
        'required': False,
    },
    {
        'id': 'labor_contract',
        'name': '劳动合同',
        'keywords': ['劳动', '合同', '劳工'],
        'exclude_keywords': ['养老', '医疗', '工伤', '失业', '保险', '参保'],
        'priority': 4,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'pension_insurance',
        'name': '养老保险参保证明',
        'keywords': ['养老', '养老保险'],
        'exclude_keywords': ['工伤', '失业', '医疗', '合同'],
        'priority': 5,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'work_injury_insurance',
        'name': '工伤保险参保证明',
        'keywords': ['工伤', '工伤保险'],
        'exclude_keywords': ['养老', '失业', '医疗', '合同'],
        'priority': 5,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'unemployment_insurance',
        'name': '失业保险参保证明',
        'keywords': ['失业', '失业保险'],
        'exclude_keywords': ['养老', '工伤', '医疗', '合同'],
        'priority': 5,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
    {
        'id': 'medical_insurance',
        'name': '医疗保险参保证明',
        'keywords': ['医疗', '医疗保险', '基本医疗'],
        'exclude_keywords': ['养老', '工伤', '失业', '合同'],
        'priority': 5,
        'multi': True,
        'sort_by_roster': True,
        'required': True,
    },
]

# 支持的文件扩展名
SUPPORTED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff',
                        '.doc', '.docx', '.xls', '.xlsx'}


def get_sections(mode):
    """获取指定模式的章节定义"""
    if mode == 'refund':
        return SECTIONS_REFUND
    elif mode == 'deduction':
        return SECTIONS_DEDUCTION
    else:
        raise ValueError(f'未知模式: {mode}')


def _match_score(filename, section):
    """
    计算文件名与章节的匹配分数
    返回: >0 表示匹配, 数值越大匹配度越高; 0 表示不匹配
    """
    name_lower = filename.lower()

    # 检查排除关键词
    for ex_kw in section.get('exclude_keywords', []):
        if ex_kw in filename:
            return 0

    # 检查匹配关键词
    score = 0
    matched_kws = []
    for kw in section['keywords']:
        if kw in filename:
            score += 10
            matched_kws.append(kw)

    # 额外加分：关键词在文件名开头
    for kw in matched_kws:
        if filename.startswith(kw):
            score += 5

    # 优先级加成（priority越小优先级越高，分数加成越大）
    if score > 0:
        score += (10 - section['priority'])

    return score


def match_files(folder_path, mode, roster=None):
    """
    扫描文件夹，将文件匹配到各章节

    Args:
        folder_path: 用户选择的文件夹路径
        mode: 'refund' 或 'deduction'
        roster: 花名册列表 [{'seq': int, 'name': str, 'idcard': str}], 可选

    Returns:
        dict: {
            'sections': [
                {
                    'id': str, 'name': str, 'files': [file_path, ...],
                    'matched': bool, 'required': bool, 'sort_by_roster': bool
                }, ...
            ],
            'unmatched': [file_path, ...],  # 未匹配到任何章节的文件
            'total_files': int,
            'matched_files': int,
        }
    """
    sections = get_sections(mode)
    section_results = {s['id']: {
        'id': s['id'],
        'name': s['name'],
        'files': [],
        'matched': False,
        'required': s['required'],
        'sort_by_roster': s['sort_by_roster'],
        'multi': s['multi'],
    } for s in sections}

    unmatched = []
    total_files = 0

    # 递归扫描文件夹
    all_files = []
    for root, dirs, files in os.walk(folder_path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            file_path = os.path.join(root, f)
            all_files.append((f, file_path))
            total_files += 1

    logger.info(f'扫描到 {total_files} 个支持文件')

    # 对每个文件计算所有章节的匹配分数
    for filename, file_path in all_files:
        best_section = None
        best_score = 0

        for section in sections:
            score = _match_score(filename, section)
            if score > best_score:
                best_score = score
                best_section = section

        if best_section and best_score > 0:
            section_results[best_section['id']]['files'].append(file_path)
            section_results[best_section['id']]['matched'] = True
            logger.debug(f'文件 {filename} -> {best_section["name"]} (score={best_score})')
        else:
            unmatched.append(file_path)
            logger.debug(f'文件 {filename} -> 未匹配')

    # 对需要按花名册排序的章节进行排序
    if roster:
        roster_names = [r['name'] for r in roster]
        for sec_id, sec_data in section_results.items():
            if sec_data['sort_by_roster'] and sec_data['files']:
                sec_data['files'] = _sort_by_roster(sec_data['files'], roster_names)

    # 按章节定义顺序返回
    ordered_sections = []
    for s in sections:
        ordered_sections.append(section_results[s['id']])

    matched_count = sum(len(s['files']) for s in ordered_sections)

    return {
        'sections': ordered_sections,
        'unmatched': unmatched,
        'total_files': total_files,
        'matched_files': matched_count,
    }


def _sort_by_roster(file_paths, roster_names):
    """按花名册顺序排序文件"""
    def get_roster_index(file_path):
        filename = os.path.basename(file_path)
        name_no_ext = os.path.splitext(filename)[0]
        for i, name in enumerate(roster_names):
            if name in name_no_ext:
                return i
        return len(roster_names)  # 未匹配到花名册的排到最后

    return sorted(file_paths, key=get_roster_index)
