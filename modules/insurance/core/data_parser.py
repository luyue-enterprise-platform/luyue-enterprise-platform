# -*- coding: utf-8 -*-
"""数据解析模块 - 从OCR文本中智能提取姓名、身份证号、险种、参保时间段"""
import re


# ============ 险种识别 ============
INSURANCE_KEYWORDS = {
    '失业保险': ['失业保险'],
    '工伤保险': ['工伤保险'],
    '养老保险': ['养老保险', '基本养老保险'],
    '医疗保险': ['医疗保险', '基本医疗'],
}

def detect_insurance_type(text):
    """
    根据OCR全文判断险种类型

    Returns:
        str: 险种名称（失业保险/工伤保险/养老保险/医疗保险），无法识别返回None
    """
    for ins_type, keywords in INSURANCE_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return ins_type
    return None


# ============ 身份证号提取与校正 ============
def fix_idcard(raw):
    """
    校正OCR误识别的身份证号
    常见错误：0→Q/O/D, 1→I/l, 2→Z, 8→B
    """
    if not raw:
        return ''
    # 去除空格和特殊字符
    s = re.sub(r'[\s\u3000]+', '', raw)
    # 替换常见OCR误识别字符
    replace_map = {
        'Q': '0', 'O': '0', 'D': '0', 'o': '0', 'q': '0',
        'I': '1', 'l': '1', 'i': '1',
        'Z': '2', 'z': '2',
        'B': '8', 'b': '6',
        'S': '5', 's': '5',
        'G': '6', 'g': '9',
    }
    result = []
    for ch in s:
        if ch.isdigit():
            result.append(ch)
        elif ch.upper() == 'X':
            result.append('X')
        elif ch.upper() in replace_map:
            result.append(replace_map[ch.upper()])
        # 其他字符跳过
    return ''.join(result)


def extract_idcard(text):
    """
    从OCR文本中提取18位身份证号

    策略：
    1. 先找"证件号码"/"身份证号"关键词附近的18位字符
    2. 再全文搜索18位数字（含可能的OCR误识别字符）
    """
    lines = text.split('\n')

    # 策略1：关键词附近搜索
    keywords = ['证件号码', '身份证号', '身份证']
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw in line:
                # 在当前行和下一行搜索
                search_text = line
                if i + 1 < len(lines):
                    search_text += ' ' + lines[i + 1]
                # 提取关键词后面的内容
                idx = search_text.find(kw)
                after = search_text[idx + len(kw):]
                after = after.lstrip(':： \t')
                # 找18位字符（数字+可能字母+X）
                m = re.search(r'[0-9A-Za-z]{17,18}', after)
                if m:
                    fixed = fix_idcard(m.group())
                    if len(fixed) == 18:
                        return fixed
                    if len(fixed) > 18:
                        return fixed[:18]

    # 策略2：全文搜索18位数字模式
    # 匹配17位数字+1位(数字或X)，允许中间混入Q/O/I等字母
    pattern = r'[0-9QOIDBIlZqobi]{17,18}[0-9Xx]'
    for line in lines:
        matches = re.findall(pattern, line)
        for m in matches:
            fixed = fix_idcard(m)
            if len(fixed) == 18:
                return fixed

    # 策略3：找任意连续18位字符
    for line in lines:
        m = re.search(r'[0-9A-Za-z]{18}', line)
        if m:
            fixed = fix_idcard(m.group())
            if len(fixed) == 18:
                return fixed

    return ''


# ============ 姓名提取 ============
def extract_name(text):
    """
    从OCR文本中提取姓名

    策略：找"姓名："或"姓名"后面的2-4个中文字符
    """
    lines = text.split('\n')

    for i, line in enumerate(lines):
        if '姓名' in line:
            # 提取"姓名"后面的内容
            idx = line.find('姓名')
            after = line[idx + 2:]
            after = after.lstrip(':： \t')
            # 匹配2-4个中文字符
            m = re.match(r'([\u4e00-\u9fa5]{2,4})', after)
            if m:
                return m.group(1)
            # 如果当前行没有，看下一行
            if i + 1 < len(lines):
                m = re.match(r'([\u4e00-\u9fa5]{2,4})', lines[i + 1].strip())
                if m:
                    return m.group(1)

    return ''


# ============ 参保时间段提取 ============
def parse_period_str(s):
    """
    将YYYYMM或YYYY-MM格式的字符串解析为(年, 月)
    支持：202401, 2024-01, 2024.01
    """
    s = s.strip().replace('-', '').replace('.', '').replace('/', '')
    if len(s) == 6 and s.isdigit():
        return int(s[:4]), int(s[4:6])
    return None


def extract_periods(text):
    """
    从OCR文本中提取所有参保时间段

    支持格式：
    - 202401-202312 / 202401~202312  (6位连写)
    - 202401202312                   (12位连写)
    - 2024年01月至2026年04月         (中文年月)
    - 2024-01至2026-04 / 2024.01-2026.04 / 2024/01~2026/04
    - 2024年1月至2026年4月           (无前导零)
    - 起始/截止日期在不同行

    Returns:
        list of (start_ym, end_ym): 如 [('2024-01', '2024-12'), ('2025-01', '2025-12')]
    """
    periods = []

    def make_period(y1, m1, y2, m2):
        """验证并创建时间段，返回 (start, end) 或 None"""
        if (1900 <= y1 <= 2100 and 1 <= m1 <= 12 and
                1900 <= y2 <= 2100 and 1 <= m2 <= 12):
            if y1 * 12 + m1 <= y2 * 12 + m2:
                return (f'{y1:04d}-{m1:02d}', f'{y2:04d}-{m2:02d}')
        return None

    # 模式1：6位连写 YYYYMM-YYYYMM 或 YYYYMM~YYYYMM
    pattern1 = r'(\d{6})\s*[-~至—–]+\s*(\d{6})'
    for m in re.finditer(pattern1, text):
        start = parse_period_str(m.group(1))
        end = parse_period_str(m.group(2))
        if start and end:
            periods.append(
                (f'{start[0]:04d}-{start[1]:02d}', f'{end[0]:04d}-{end[1]:02d}')
            )

    # 模式2：无分隔符的12位数字 YYYYMMYYYYMM
    if not periods:
        pattern2 = r'(\d{6})(\d{6})'
        for m in re.finditer(pattern2, text):
            start = parse_period_str(m.group(1))
            end = parse_period_str(m.group(2))
            if start and end:
                p = make_period(start[0], start[1], end[0], end[1])
                if p:
                    periods.append(p)

    # 模式3：带分隔符的日期对 YYYY[.-/年]MM 至/-/YYYY[.-/年]MM
    if not periods:
        # 日期格式：4位年 + 分隔符 + 1~2位月 (+ 可选"月"字)
        date_pat = r'(\d{4})\s*[-./年]\s*(\d{1,2})\s*月?'
        # 时间段：日期 + 分隔(至/-/~) + 日期
        period_pat = date_pat + r'\s*[-~至—–]+\s*' + date_pat
        for m in re.finditer(period_pat, text):
            p = make_period(int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)))
            if p:
                periods.append(p)

    # 模式4：纯中文格式 "XXXX年XX月至XXXX年XX月"
    if not periods:
        pattern4 = r'(\d{4})\s*年\s*(\d{1,2})\s*月?\s*[至\-~—–]+\s*(\d{4})\s*年\s*(\d{1,2})\s*月?'
        for m in re.finditer(pattern4, text):
            p = make_period(int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)))
            if p:
                periods.append(p)

    # 模式5：跨行 — 起始/截止在不同行（如"起始时间：2024年01月" / "截止时间：2026年04月"）
    if not periods:
        lines = text.split('\n')
        start_ym = None
        end_ym = None
        for i, line in enumerate(lines):
            search_text = line
            if i + 1 < len(lines):
                search_text += ' ' + lines[i + 1]
            if any(kw in line for kw in ['起始', '开始', '起期', '起止']):
                m = re.search(r'(\d{4})\s*[-./年]\s*(\d{1,2})', search_text)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 1900 <= y <= 2100 and 1 <= mo <= 12:
                        start_ym = (y, mo)
            if any(kw in line for kw in ['截止', '结束', '止期', '终止']):
                m = re.search(r'(\d{4})\s*[-./年]\s*(\d{1,2})', search_text)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 1900 <= y <= 2100 and 1 <= mo <= 12:
                        end_ym = (y, mo)
        if start_ym and end_ym:
            p = make_period(start_ym[0], start_ym[1], end_ym[0], end_ym[1])
            if p:
                periods.append(p)

    return periods


def get_full_period(text):
    """
    从OCR文本中提取完整参保时间段（取所有时间段的最早起始和最晚截止）

    Returns:
        tuple: (start_ym, end_ym) 如 ('2024-01', '2026-04')，无法提取返回None
    """
    periods = extract_periods(text)
    if not periods:
        return None

    def ym_key(ym):
        y, m = map(int, ym.split('-'))
        return y * 12 + m

    earliest_start = min(periods, key=lambda p: ym_key(p[0]))[0]
    latest_end = max(periods, key=lambda p: ym_key(p[1]))[1]

    return earliest_start, latest_end


# ============ 缴费单位提取 ============
def extract_company_name(text):
    """
    从OCR文本中提取缴费单位/参保单位名称

    策略：查找"缴费单位"或"参保单位"关键词，提取后面的单位名称

    Returns:
        str: 单位名称，未找到返回空字符串
    """
    lines = text.split('\n')
    # 长关键词优先，避免"缴费单位名称"被"缴费单位"误匹配
    keywords = ['缴费单位名称', '参保单位名称', '缴费单位', '参保单位',
                '用人单位', '单位名称', '单位:', '单位：']

    for i, line in enumerate(lines):
        for kw in keywords:
            if kw in line:
                # 提取关键词后面的内容
                idx = line.find(kw)
                after = line[idx + len(kw):]
                after = after.lstrip(':： \t')
                # 取非空白内容，通常单位名是中文
                m = re.match(r'([\u4e00-\u9fa5\w\(\)（）\u00b7·]{2,40})', after)
                if m:
                    return m.group(1)
                # 如果当前行后面内容不够，看下一行
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    m = re.match(r'([\u4e00-\u9fa5\w\(\)（）\u00b7·]{2,40})', next_line)
                    if m:
                        return m.group(1)

    return ''


# ============ 综合解析 ============
def parse_ocr_result(text):
    """
    从OCR文本中提取全部关键字段

    Args:
        text: OCR识别的全文文本

    Returns:
        dict: {
            'insurance_type': 险种名称,
            'name': 姓名,
            'idcard': 身份证号,
            'period': (start_ym, end_ym) 或 None,
            'raw_text': 原始文本
        }
    """
    result = {
        'insurance_type': detect_insurance_type(text),
        'name': extract_name(text),
        'idcard': extract_idcard(text),
        'period': get_full_period(text),
        'company_name': extract_company_name(text),
        'raw_text': text,
    }
    return result


def group_by_person(records):
    """
    将多条OCR解析记录按人员分组（姓名+身份证号）

    Args:
        records: list of parse_ocr_result()返回的dict

    Returns:
        list of dict: 每个元素代表一个人，包含：
            'name': 姓名
            'idcard': 身份证号
            'insurances': {险种: (start, end)}
    """
    persons = {}

    for rec in records:
        if not rec['name'] or not rec['insurance_type']:
            continue

        # 用姓名作为分组key（身份证号可能OCR不准）
        key = rec['name']
        if key not in persons:
            persons[key] = {
                'name': rec['name'],
                'idcard': rec['idcard'],
                'insurances': {}
            }
        else:
            # 更新身份证号（如果当前为空）
            if not persons[key]['idcard'] and rec['idcard']:
                persons[key]['idcard'] = rec['idcard']

        ins_type = rec['insurance_type']
        period = rec['period']
        if period:
            # 如果同一险种有多条记录，取并集（最早起始到最晚截止）
            if ins_type in persons[key]['insurances']:
                old_start, old_end = persons[key]['insurances'][ins_type]
                def ym_key(ym):
                    y, m = map(int, ym.split('-'))
                    return y * 12 + m
                new_start = min(old_start, period[0], key=ym_key)
                new_end = max(old_end, period[1], key=ym_key)
                persons[key]['insurances'][ins_type] = (new_start, new_end)
            else:
                persons[key]['insurances'][ins_type] = period

    return list(persons.values())
