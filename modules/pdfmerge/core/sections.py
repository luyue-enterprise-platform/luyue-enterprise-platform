# -*- coding: utf-8 -*-
"""章节定义与 OCR 智能匹配引擎

核心逻辑：
1. 所有文件已转为图片，每张图片有 OCR 文本
2. 将 OCR 文本 + 文件名与章节名目关键词智能匹配
3. 匹配的图片放入"图片池"，不匹配的忽略
4. 按退税/抵税模板顺序排列，支持多年份分组
5. 生成有序的章节列表供 PDF 构建
"""
import os
import re
import logging

logger = logging.getLogger('pdfmerge.sections')

# ============================================================
# 章节模板定义
# ============================================================

# 退税模式章节（按用户指定顺序）
# per_year=True 的章节会按年份重复
SECTIONS_REFUND = [
    # --- 固定章节（非按年重复）---
    {
        'id': 'refund_application',
        'name': '退税申请',
        'keywords': ['退税申请'],
        'weak_keywords': ['退税'],
        'exclude_keywords': [],
        'per_year': False,
        'priority': 1,
        'required': True,
    },
    {
        'id': 'general_ledger',
        'name': '申报重点群体优惠政策总台账',
        'keywords': ['总台账', '重点群体.*台账', '优惠政策.*总台账'],
        'weak_keywords': ['总台账'],
        'exclude_keywords': ['年度台账', '减免税'],
        'per_year': False,
        'priority': 2,
        'required': True,
    },
    {
        'id': 'employment_cert',
        'name': '就业认定证明',
        'keywords': ['就业认定', '认定证明'],
        'weak_keywords': ['就业.*认定', '认定.*证明'],
        'exclude_keywords': ['脱贫', '退役', '劳动合同', '就业信息表'],
        'per_year': False,
        'priority': 3,
        'required': True,
    },
    {
        'id': 'poverty_cert',
        'name': '脱贫人口人员身份类型证明',
        'keywords': ['脱贫人口', '脱贫.*身份', '身份类型.*脱贫'],
        'weak_keywords': ['脱贫'],
        'exclude_keywords': ['退役', '劳动合同'],
        'per_year': False,
        'priority': 4,
        'required': True,
    },
    {
        'id': 'veteran_cert',
        'name': '自主就业退役士兵身份证明',
        'keywords': ['退役士兵', '自主就业.*退役', '退役.*身份'],
        'weak_keywords': ['退役士兵', '退役'],
        'exclude_keywords': ['脱贫', '劳动合同'],
        'per_year': False,
        'priority': 5,
        'required': True,
    },
    # --- 按年重复章节 ---
    {
        'id': 'original_vat',
        'name': '原增值税申报表',
        'keywords': ['增值税.*申报表', '增值税纳税申报'],
        'weak_keywords': ['增值税'],
        'exclude_keywords': ['修改', '减免税', '明细'],
        'per_year': True,
        'priority': 6,
        'required': True,
    },
    {
        'id': 'modified_vat',
        'name': '修改后增值税申报表',
        'keywords': ['修改.*增值税', '增值税.*修改', '更正.*增值税'],
        'weak_keywords': ['修改.*申报'],
        'exclude_keywords': ['减免税', '明细'],
        'per_year': True,
        'priority': 7,
        'required': False,
    },
    {
        'id': 'annual_ledger',
        'name': '申报重点群体税收优惠政策年度台账',
        'keywords': ['年度台账', '重点群体.*年度', '税收优惠.*年度.*台账'],
        'weak_keywords': ['年度台账'],
        'exclude_keywords': ['总台账'],
        'per_year': True,
        'priority': 8,
        'required': True,
    },
    {
        'id': 'employment_info',
        'name': '申报重点群体或自主就业退役士兵就业信息表',
        'keywords': ['就业信息表', '重点群体.*就业.*信息', '退役士兵.*就业.*信息'],
        'weak_keywords': ['就业信息表'],
        'exclude_keywords': ['认定证明', '劳动合同'],
        'per_year': True,
        'priority': 9,
        'required': True,
    },
    {
        'id': 'tax_paid_cert',
        'name': '完税证明',
        'keywords': ['完税证明', '完税证', '税收完税'],
        'weak_keywords': ['完税'],
        'exclude_keywords': ['申报表', '台账'],
        'per_year': True,
        'priority': 10,
        'required': True,
    },
    # --- 固定章节（放在所有年份之后）---
    {
        'id': 'labor_contract',
        'name': '脱贫人口及自主就业退役士兵1年及以上劳动合同',
        'keywords': ['劳动合同', '劳动.*合同'],
        'weak_keywords': ['合同'],
        'exclude_keywords': ['参保证明', '申报表', '台账', '完税', '认定'],
        'per_year': False,
        'priority': 11,
        'required': True,
        'sort_by_name': True,
    },
    {
        'id': 'pension_insurance',
        'name': '养老保险参保证明',
        'keywords': ['养老.*参保', '养老保险.*参保', '养老.*缴费证明'],
        'weak_keywords': ['养老'],
        'exclude_keywords': ['工伤', '失业', '医疗', '生育'],
        'per_year': False,
        'priority': 12,
        'required': True,
    },
    {
        'id': 'work_injury_insurance',
        'name': '工伤保险参保证明',
        'keywords': ['工伤.*参保', '工伤保险.*参保', '工伤.*缴费证明'],
        'weak_keywords': ['工伤'],
        'exclude_keywords': ['养老', '失业', '医疗', '生育'],
        'per_year': False,
        'priority': 13,
        'required': True,
    },
    {
        'id': 'unemployment_insurance',
        'name': '失业保险参保证明',
        'keywords': ['失业.*参保', '失业保险.*参保', '失业.*缴费证明'],
        'weak_keywords': ['失业'],
        'exclude_keywords': ['养老', '工伤', '医疗', '生育'],
        'per_year': False,
        'priority': 14,
        'required': True,
    },
    {
        'id': 'medical_insurance',
        'name': '医疗保险参保证明',
        'keywords': ['医疗.*参保', '医疗保险.*参保', '医疗.*缴费证明', '职工.*医保'],
        'weak_keywords': ['医疗', '医保'],
        'exclude_keywords': ['养老', '工伤', '失业', '生育'],
        'per_year': False,
        'priority': 15,
        'required': True,
    },
]

# 抵税模式章节（按用户指定顺序）
SECTIONS_DEDUCTION = [
    # --- 按年重复章节 ---
    {
        'id': 'situation_explanation',
        'name': '情况说明',
        'keywords': ['情况说明'],
        'weak_keywords': ['情况.*说明'],
        'exclude_keywords': ['申报表', '台账', '认定', '证明'],
        'per_year': True,
        'priority': 1,
        'required': True,
    },
    {
        'id': 'vat_reduction_detail',
        'name': '增值税减免税明细申报表',
        'keywords': ['减免税.*明细.*申报', '减免税.*申报表', '增值税.*减免.*明细'],
        'weak_keywords': ['减免税.*明细', '减免税.*申报'],
        'exclude_keywords': ['修改', '更正'],
        'per_year': True,
        'priority': 2,
        'required': True,
    },
    {
        'id': 'deduction_ledger',
        'name': '申报重点群体优惠政策台账',
        'keywords': ['重点群体.*台账', '优惠政策.*台账', '重点群体.*优惠.*台账'],
        'weak_keywords': ['台账'],
        'exclude_keywords': ['总台账', '年度台账', '减免税'],
        'per_year': True,
        'priority': 3,
        'required': True,
    },
    {
        'id': 'employment_info_deduction',
        'name': '重点群体或自主就业退役士兵就业信息表',
        'keywords': ['就业信息表', '重点群体.*就业.*信息', '退役士兵.*就业.*信息'],
        'weak_keywords': ['就业信息表'],
        'exclude_keywords': ['认定证明', '劳动合同'],
        'per_year': True,
        'priority': 4,
        'required': True,
    },
    # --- 固定章节 ---
    {
        'id': 'employment_cert_deduction',
        'name': '企业吸纳重点群体就业认定证明',
        'keywords': ['企业吸纳.*就业.*认定', '吸纳.*认定证明', '就业认定证明'],
        'weak_keywords': ['就业认定', '认定证明'],
        'exclude_keywords': ['脱贫', '退役', '劳动合同', '就业信息表'],
        'per_year': False,
        'priority': 5,
        'required': True,
    },
    {
        'id': 'poverty_cert_deduction',
        'name': '脱贫人口人员身份类型证明',
        'keywords': ['脱贫人口', '脱贫.*身份', '身份类型.*脱贫'],
        'weak_keywords': ['脱贫'],
        'exclude_keywords': ['退役', '劳动合同'],
        'per_year': False,
        'priority': 6,
        'required': True,
    },
    {
        'id': 'veteran_cert_deduction',
        'name': '自主就业退役士兵身份证明',
        'keywords': ['退役士兵', '自主就业.*退役', '退役.*身份'],
        'weak_keywords': ['退役士兵', '退役'],
        'exclude_keywords': ['脱贫', '劳动合同'],
        'per_year': False,
        'priority': 7,
        'required': True,
    },
    # --- 固定章节（放在所有年份之后）---
    {
        'id': 'labor_contract_deduction',
        'name': '脱贫人口及自主就业退役士兵1年及以上劳动合同',
        'keywords': ['劳动合同', '劳动.*合同'],
        'weak_keywords': ['合同'],
        'exclude_keywords': ['参保证明', '申报表', '台账', '完税', '认定', '情况说明'],
        'per_year': False,
        'priority': 8,
        'required': True,
        'sort_by_name': True,
    },
    {
        'id': 'pension_insurance_deduction',
        'name': '养老保险参保证明',
        'keywords': ['养老.*参保', '养老保险.*参保', '养老.*缴费证明'],
        'weak_keywords': ['养老'],
        'exclude_keywords': ['工伤', '失业', '医疗', '生育'],
        'per_year': False,
        'priority': 9,
        'required': True,
    },
    {
        'id': 'work_injury_insurance_deduction',
        'name': '工伤保险参保证明',
        'keywords': ['工伤.*参保', '工伤保险.*参保', '工伤.*缴费证明'],
        'weak_keywords': ['工伤'],
        'exclude_keywords': ['养老', '失业', '医疗', '生育'],
        'per_year': False,
        'priority': 10,
        'required': True,
    },
    {
        'id': 'unemployment_insurance_deduction',
        'name': '失业保险参保证明',
        'keywords': ['失业.*参保', '失业保险.*参保', '失业.*缴费证明'],
        'weak_keywords': ['失业'],
        'exclude_keywords': ['养老', '工伤', '医疗', '生育'],
        'per_year': False,
        'priority': 11,
        'required': True,
    },
    {
        'id': 'medical_insurance_deduction',
        'name': '医疗保险参保证明',
        'keywords': ['医疗.*参保', '医疗保险.*参保', '医疗.*缴费证明', '职工.*医保'],
        'weak_keywords': ['医疗', '医保'],
        'exclude_keywords': ['养老', '工伤', '失业', '生育'],
        'per_year': False,
        'priority': 12,
        'required': True,
    },
]


def get_section_templates(mode):
    """获取指定模式的章节模板"""
    if mode == 'refund':
        return SECTIONS_REFUND
    elif mode == 'deduction':
        return SECTIONS_DEDUCTION
    else:
        raise ValueError(f'未知模式: {mode}')


# ============================================================
# 年份提取
# ============================================================

def _extract_year(text):
    """从文本中提取年份

    查找 4 位数字（2000-2099），优先取"所属期"附近的年份

    Returns:
        int: 年份，未找到返回 None
    """
    if not text:
        return None

    # 优先匹配 "所属期" 附近的年份
    period_match = re.search(r'所属期[：:]*\s*(\d{4})', text)
    if period_match:
        year = int(period_match.group(1))
        if 2000 <= year <= 2099:
            return year

    # 匹配 "XXXX年" 格式
    year_matches = re.findall(r'(20\d{2})年', text)
    if year_matches:
        year = int(year_matches[0])
        if 2000 <= year <= 2099:
            return year

    # 匹配独立的 4 位年份
    year_matches = re.findall(r'\b(20\d{2})\b', text)
    if year_matches:
        year = int(year_matches[0])
        if 2000 <= year <= 2099:
            return year

    return None


def _extract_period_text(text):
    """提取时间段文本（如 '2023年1月-2023年12月'）

    Returns:
        str: 时间段文本，未找到返回 None
    """
    if not text:
        return None

    # 匹配 "XXXX年X月至XXXX年X月" 或 "XXXX年X月-XXXX年X月"
    patterns = [
        r'(\d{4}年\d{1,2}月)\s*[至到\-~—]+\s*(\d{4}年\d{1,2}月)',
        r'(\d{4}\.\d{1,2})\s*[至到\-~—]+\s*(\d{4}\.\d{1,2})',
        r'(\d{4}\d{2})\s*[至到\-~—]+\s*(\d{4}\d{2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return f'{match.group(1)}-{match.group(2)}'

    return None


# ============================================================
# 匹配评分
# ============================================================

def _check_keywords(text, keywords, use_regex=False):
    """检查文本中是否包含关键词

    Args:
        text: 要搜索的文本
        keywords: 关键词列表
        use_regex: 是否使用正则匹配

    Returns:
        list[str]: 匹配到的关键词列表
    """
    matched = []
    for kw in keywords:
        if use_regex:
            if re.search(kw, text):
                matched.append(kw)
        else:
            if kw in text:
                matched.append(kw)
    return matched


def _calc_score(filename, ocr_text, section):
    """计算图片与章节的匹配分数

    评分规则：
    - 文件名强关键词命中: +15 分/个
    - OCR文本强关键词命中: +10 分/个
    - 文件名弱关键词命中: +5 分/个
    - OCR文本弱关键词命中: +3 分/个
    - 排除关键词命中: 直接返回 0
    - 文件名开头命中: +5 额外分

    Returns:
        tuple: (score, matched_keywords)
    """
    filename_lower = filename.lower() if filename else ''
    ocr_lower = ocr_text or ''

    # 检查排除关键词（文件名和OCR文本都检查）
    for ek in section.get('exclude_keywords', []):
        if re.search(ek, filename_lower) or re.search(ek, ocr_lower):
            return 0, []

    score = 0
    matched_kws = []

    # 强关键词匹配（使用正则）
    for kw in section.get('keywords', []):
        in_filename = bool(re.search(kw, filename_lower))
        in_ocr = bool(re.search(kw, ocr_lower))

        if in_filename:
            score += 15
            matched_kws.append(f'F:{kw}')
        if in_ocr:
            score += 10
            matched_kws.append(f'O:{kw}')

        # 文件名开头命中额外加分
        if in_filename and filename_lower.startswith(kw.split('.*')[0]):
            score += 5

    # 弱关键词匹配
    for wkw in section.get('weak_keywords', []):
        in_filename = bool(re.search(wkw, filename_lower))
        in_ocr = bool(re.search(wkw, ocr_lower))

        if in_filename:
            score += 5
            matched_kws.append(f'FW:{wkw}')
        if in_ocr:
            score += 3
            matched_kws.append(f'OW:{wkw}')

    return score, matched_kws


# ============================================================
# 核心匹配函数
# ============================================================

def match_images(images, mode):
    """将图片列表与章节模板智能匹配

    Args:
        images: list[dict], 每个元素包含:
            - id: 图片唯一ID
            - path: 图片文件路径
            - original_filename: 原始文件名
            - ocr_text: OCR识别文本
        mode: 'refund' 或 'deduction'

    Returns:
        dict: {
            'sections': [
                {
                    'id': 'section_id',
                    'name': '章节名称',
                    'year': 2023 or None,
                    'images': [image_dict, ...],
                    'required': True/False,
                    'matched': True/False,
                    'per_year': True/False,
                },
                ...
            ],
            'unmatched': [image_dict, ...],
        }
    """
    templates = get_section_templates(mode)

    # 为每张图片计算所有章节的匹配分数
    image_matches = []  # [(image, best_section_id, best_score, year), ...]

    for img in images:
        filename = img.get('original_filename', '')
        ocr_text = img.get('ocr_text', '')

        best_section = None
        best_score = 0
        best_year = None

        for tmpl in templates:
            score, kws = _calc_score(filename, ocr_text, tmpl)
            if score > best_score:
                best_score = score
                best_section = tmpl['id']
                # 提取年份
                if tmpl.get('per_year'):
                    best_year = _extract_year(ocr_text) or _extract_year(filename)
                else:
                    best_year = None

        if best_section and best_score > 0:
            image_matches.append((img, best_section, best_score, best_year))
            logger.debug(f'匹配: {filename} → {best_section} (分数:{best_score}, 年份:{best_year})')
        else:
            image_matches.append((img, None, 0, None))
            logger.debug(f'未匹配: {filename}')

    # 构建有序章节列表
    result_sections = []
    matched_image_ids = set()

    # 分离固定章节和按年章节
    fixed_before = [t for t in templates if not t.get('per_year') and t['priority'] <= _get_first_year_priority(mode)]
    year_sections = [t for t in templates if t.get('per_year')]
    fixed_after = [t for t in templates if not t.get('per_year') and t['priority'] > _get_first_year_priority(mode)]

    # 1. 添加固定章节（前）
    for tmpl in fixed_before:
        section_images = []
        for img, sid, score, year in image_matches:
            if sid == tmpl['id'] and img['id'] not in matched_image_ids:
                section_images.append(img)
                matched_image_ids.add(img['id'])

        # 排序
        if tmpl.get('sort_by_name'):
            section_images.sort(key=lambda x: x.get('original_filename', ''))

        result_sections.append({
            'id': tmpl['id'],
            'name': tmpl['name'],
            'year': None,
            'images': section_images,
            'required': tmpl.get('required', False),
            'matched': len(section_images) > 0,
            'per_year': False,
        })

    # 2. 收集所有年份并排序
    years_found = set()
    for img, sid, score, year in image_matches:
        if sid in [t['id'] for t in year_sections] and year is not None:
            years_found.add(year)

    # 也检查没有年份的按年章节图片
    unnamed_year_images = []
    for img, sid, score, year in image_matches:
        if sid in [t['id'] for t in year_sections] and year is None and img['id'] not in matched_image_ids:
            unnamed_year_images.append((img, sid, score))

    # 按年份顺序处理
    for year in sorted(years_found):
        for tmpl in year_sections:
            section_images = []
            for img, sid, score, img_year in image_matches:
                if sid == tmpl['id'] and img_year == year and img['id'] not in matched_image_ids:
                    section_images.append(img)
                    matched_image_ids.add(img['id'])

            if section_images:
                year_label = f'{year}年' if year else '未知年份'
                result_sections.append({
                    'id': tmpl['id'],
                    'name': f'{tmpl["name"]}（{year_label}）',
                    'year': year,
                    'images': section_images,
                    'required': tmpl.get('required', False),
                    'matched': True,
                    'per_year': True,
                })

    # 处理未提取到年份的按年章节图片
    if unnamed_year_images:
        for tmpl in year_sections:
            section_images = []
            for img, sid, score in unnamed_year_images:
                if sid == tmpl['id'] and img['id'] not in matched_image_ids:
                    section_images.append(img)
                    matched_image_ids.add(img['id'])

            if section_images:
                result_sections.append({
                    'id': tmpl['id'],
                    'name': f'{tmpl["name"]}（未知年份）',
                    'year': None,
                    'images': section_images,
                    'required': tmpl.get('required', False),
                    'matched': True,
                    'per_year': True,
                })

    # 3. 添加固定章节（后）
    for tmpl in fixed_after:
        section_images = []
        for img, sid, score, year in image_matches:
            if sid == tmpl['id'] and img['id'] not in matched_image_ids:
                section_images.append(img)
                matched_image_ids.add(img['id'])

        # 排序
        if tmpl.get('sort_by_name'):
            section_images.sort(key=lambda x: x.get('original_filename', ''))

        result_sections.append({
            'id': tmpl['id'],
            'name': tmpl['name'],
            'year': None,
            'images': section_images,
            'required': tmpl.get('required', False),
            'matched': len(section_images) > 0,
            'per_year': False,
        })

    # 4. 收集未匹配的图片
    unmatched = [img for img, sid, score, year in image_matches if sid is None]

    logger.info(f'匹配完成: {len(matched_image_ids)}/{len(images)} 张图片已匹配, '
                f'{len(unmatched)} 张未匹配, {len(result_sections)} 个章节')

    return {
        'sections': result_sections,
        'unmatched': unmatched,
    }


def _get_first_year_priority(mode):
    """获取第一个按年章节的优先级，用于区分固定章节的前后位置"""
    templates = get_section_templates(mode)
    for t in templates:
        if t.get('per_year'):
            return t['priority'] - 1
    return len(templates)
