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


def _calc_name_score(name, section):
    """
    计算名称与章节关键词的匹配分数（不含排除词检查）
    返回: >0 表示匹配, 数值越大匹配度越高; 0 表示不匹配
    """
    score = 0
    matched_kws = []
    for kw in section['keywords']:
        if kw in name:
            score += 10
            matched_kws.append(kw)

    # 额外加分：关键词在名称开头
    for kw in matched_kws:
        if name.startswith(kw):
            score += 5

    # 优先级加成（priority越小优先级越高，分数加成越大）
    if score > 0:
        score += (10 - section['priority'])

    return score


def _match_score(filename, section, folder_names=None):
    """
    计算文件名/文件夹名与章节的匹配分数

    匹配规则：
    - 文件名排除关键词命中 → 直接返回0（该文件不属于此章节）
    - 文件名匹配 → 基于文件名计算分数
    - 文件夹名匹配 → 基于文件夹名计算分数（文件夹名排除关键词命中则跳过该文件夹名）
    - 取文件名分数和文件夹名分数的最大值

    Args:
        filename: 文件名
        section: 章节定义
        folder_names: 文件所在的文件夹名称列表（从近到远），可选

    Returns: >0 表示匹配, 数值越大匹配度越高; 0 表示不匹配
    """
    # 1. 检查文件名排除关键词 — 命中则直接返回0
    for ex_kw in section.get('exclude_keywords', []):
        if ex_kw in filename:
            return 0

    # 2. 计算文件名匹配分数
    file_score = _calc_name_score(filename, section)

    # 3. 计算文件夹名匹配分数
    folder_score = 0
    if folder_names:
        for folder_name in folder_names:
            # 检查文件夹名排除关键词 — 命中则跳过该文件夹名
            folder_excluded = False
            for ex_kw in section.get('exclude_keywords', []):
                if ex_kw in folder_name:
                    folder_excluded = True
                    break
            if folder_excluded:
                continue

            score = _calc_name_score(folder_name, section)
            if score > folder_score:
                folder_score = score

    return max(file_score, folder_score)


def _normalize_path(path):
    """
    规范化文件路径，处理Windows长路径问题
    Windows默认MAX_PATH=260，超过此长度的路径需要使用 \\\\?\\ 前缀
    """
    # 规范化路径分隔符
    path = os.path.normpath(path)
    # Windows长路径支持
    if os.name == 'nt':
        # 转为绝对路径
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        # 如果路径超过255字符且没有长路径前缀，添加前缀
        if len(path) > 255 and not path.startswith('\\\\?\\'):
            path = '\\\\?\\' + path
    return path


def _match_core(all_files, mode, roster=None):
    """
    核心匹配逻辑：将 (filename, file_path, folder_names) 列表匹配到各章节

    Args:
        all_files: [(filename, file_path, folder_names), ...] 列表
                   folder_names: 文件所在文件夹名称列表（从近到远），可为空列表
        mode: 'refund' 或 'deduction'
        roster: 花名册列表 [{'seq': int, 'name': str, 'idcard': str}], 可选

    Returns:
        dict: 同 match_files 返回格式
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
    total_files = len(all_files)

    # 对每个文件计算所有章节的匹配分数（同时考虑文件名和文件夹名）
    for item in all_files:
        filename, file_path, folder_names = item

        best_section = None
        best_score = 0

        for section in sections:
            score = _match_score(filename, section, folder_names)
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
    # 递归扫描文件夹（包含子文件夹）
    all_files = []
    walk_errors = []

    def _on_walk_error(err):
        """os.walk 错误回调：记录目录访问错误"""
        walk_errors.append(str(err))
        logger.warning(f'目录访问错误: {err}')

    for root, dirs, files in os.walk(folder_path, onerror=_on_walk_error):
        rel_dir = os.path.relpath(root, folder_path)
        logger.info(f'扫描目录: {rel_dir} (找到 {len(files)} 个文件)')

        # 提取相对文件夹名称列表（从近到远）
        # 例: rel_dir = "劳动合同\\张三" → folder_names = ["张三", "劳动合同"]
        # rel_dir = "." (根目录) → folder_names = []
        if rel_dir == '.':
            folder_names = []
        else:
            folder_names = rel_dir.split(os.sep)
            # 反转: 从近到远 → 从远到近（远的文件夹更可能是分类文件夹）
            # 但评分时取最高分，顺序不影响结果
            folder_names = list(reversed(folder_names))

        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            file_path = os.path.join(root, f)
            # 验证文件可访问
            if not os.path.isfile(file_path):
                # 尝试长路径前缀
                long_path = _normalize_path(file_path)
                if long_path != file_path and os.path.isfile(long_path):
                    file_path = long_path
                else:
                    logger.warning(f'文件无法访问，跳过: {file_path}')
                    continue
            all_files.append((f, file_path, folder_names))
            logger.debug(f'  发现文件: {os.path.relpath(file_path, folder_path)}')

    if walk_errors:
        logger.warning(f'扫描完成，但有 {len(walk_errors)} 个目录访问错误')
    logger.info(f'扫描到 {len(all_files)} 个支持文件 (来自 {folder_path})')

    return _match_core(all_files, mode, roster)


def match_files_from_paths(file_paths, mode, roster=None):
    """
    从文件路径列表匹配文件到各章节（不需要文件夹扫描）

    用于前端传入多个文件夹/文件选择后的合并文件路径列表。
    同时支持文件名匹配和文件夹名称匹配。

    Args:
        file_paths: 绝对文件路径列表
        mode: 'refund' 或 'deduction'
        roster: 花名册列表, 可选

    Returns:
        dict: 同 match_files 返回格式
    """
    all_files = []
    for fp in file_paths:
        # 验证文件可访问
        if not os.path.isfile(fp):
            long_path = _normalize_path(fp)
            if long_path != fp and os.path.isfile(long_path):
                fp = long_path
            else:
                logger.warning(f'文件不存在，跳过: {fp}')
                continue
        filename = os.path.basename(fp)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            logger.debug(f'文件格式不支持，跳过: {fp}')
            continue

        # 提取父文件夹名称（从近到远，最多3级）
        # 例: C:\\data\\劳动合同\\张三.pdf → folder_names = ["劳动合同", "data"]
        # 例: C:\\data\\劳动合同\\2024年\\李四.pdf → folder_names = ["2024年", "劳动合同", "data"]
        folder_names = []
        parent_dir = os.path.dirname(fp)
        for _ in range(3):
            if not parent_dir:
                break
            folder_name = os.path.basename(parent_dir)
            if folder_name:
                folder_names.append(folder_name)
            new_parent = os.path.dirname(parent_dir)
            if new_parent == parent_dir:  # 已到根目录
                break
            parent_dir = new_parent

        all_files.append((filename, fp, folder_names))

    logger.info(f'从路径列表匹配 {len(all_files)} 个支持文件')
    return _match_core(all_files, mode, roster)


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
