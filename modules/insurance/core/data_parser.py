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
# 字段标签词黑名单——OCR漏识别姓名值时，"姓名："后面紧跟的往往是下一个字段标签，
# 绝不能把标签当成姓名（v1.1.47 修复：姓名被误识别成"身份证号"）
_NAME_LABEL_WORDS = {
    '身份证号', '身份证', '证件号码', '证件号', '号码', '个人编号', '编号',
    '参保状态', '状态', '性别', '民族', '出生日期', '出生', '住址', '地址',
    '单位名称', '公司名称', '单位', '公司', '缴费年度', '年度', '缴费月份', '月份',
    '缴费月数', '总缴费月数', '月数', '日期', '时间', '经办机构', '机构',
    '参保证明', '证明', '姓名', '居民',
}
_NAME_LABEL_KEYWORDS = ('证号', '证件', '编号', '缴费', '参保', '单位',
                        '公司', '年度', '月份', '机构', '状态', '证明', '险种')


def _is_label_word(s):
    """判断提取到的"姓名"是否其实是字段标签词（防标签误当姓名）"""
    if not s:
        return True
    if s in _NAME_LABEL_WORDS:
        return True
    for kw in _NAME_LABEL_KEYWORDS:
        if kw in s:
            return True
    return False


def extract_name(text):
    """
    从OCR文本中提取姓名

    策略：
    1. 主策略：找"姓名："或"姓名"后面的2-4个中文字符（拒绝字段标签词；
       若贪婪匹配吞进"身份证"等标签前缀如"张三身份"，回退标签后缀）
    2. 兜底1：OCR合并行——姓名值紧贴在"身份证"标签之前（如"姓名：张三身份证号610..."）
    3. 兜底2："姓名"下一行的前导中文姓名（须后随空白或行尾，
       防止误取"现缴费单位名称：..."这类长标签的前4个字）
    """
    lines = text.split('\n')

    for i, line in enumerate(lines):
        # OCR常把"姓 名"拆成带空格的形式，去空格后再找标签
        norm = line.replace(' ', '').replace('\u3000', '')
        if '姓名' not in norm:
            continue
        # 主策略：提取"姓名"后面的内容
        idx = norm.find('姓名')
        after = norm[idx + 2:]
        after = after.lstrip(':： \t')
        # 匹配2-4个中文字符（且不是字段标签词）
        m = re.match(r'([\u4e00-\u9fa5]{2,4})', after)
        if m:
            word = m.group(1)
            # 贪婪匹配可能吞进标签前缀（如"张三身份"），回退标签后缀
            changed = True
            while changed and word:
                changed = False
                for suffix in ('身份证', '证件', '身份', '编号', '号码', '证', '身'):
                    if word.endswith(suffix) and len(word) - len(suffix) >= 2:
                        word = word[:-len(suffix)]
                        changed = True
                        break
            if word and not _is_label_word(word):
                return word
        # 兜底1：姓名值与"身份证"标签连在一起的合并行
        m = re.search(r'([\u4e00-\u9fa5]{2,4})(?=身份证)', norm)
        if m and not _is_label_word(m.group(1)):
            return m.group(1)
        # 兜底2：看下一行（须为独立/前导姓名词）
        if i + 1 < len(lines):
            m = re.match(r'([\u4e00-\u9fa5]{2,4})(?=\s|$)', lines[i + 1].strip())
            if m and not _is_label_word(m.group(1)):
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


def _ym_key(ym):
    """将 'YYYY-MM' 转为可比较的整数"""
    y, m = map(int, ym.split('-'))
    return y * 12 + m


def _keep_latest_segment(periods):
    """
    从多个时间段中，只保留最近的一段连续时间段。

    将重叠或相邻（首尾相接）的时间段合并为一段，
    然后返回结束时间最晚的那一段，其余忽略。

    例如：
        [('2020-01','2021-06'), ('2024-01','2026-06')]
        → [('2024-01', '2026-06')]   # 2020-2021 被忽略

        [('2024-01','2024-12'), ('2025-01','2026-06')]
        → [('2024-01', '2026-06')]   # 相邻合并为一段

    Args:
        periods: list of (start_ym, end_ym)

    Returns:
        list of (start_ym, end_ym)：只包含最近一段，空列表表示无时间段
    """
    if not periods:
        return []

    # 按 start 排序
    sorted_periods = sorted(periods, key=lambda p: _ym_key(p[0]))

    # 合并重叠/相邻时间段
    merged = []
    cur_start, cur_end = sorted_periods[0]

    for i in range(1, len(sorted_periods)):
        s, e = sorted_periods[i]
        if _ym_key(s) <= _ym_key(cur_end) + 1:
            # 相邻或重叠，合并
            cur_end = max(cur_end, e, key=_ym_key)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    merged.append((cur_start, cur_end))

    # 返回结束时间最晚的一段
    return [max(merged, key=lambda p: _ym_key(p[1]))]


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

    # 模式5：跨行/同行 — 起始/截止关键词 + 日期
    # 支持：同行 "起始时间：2024年01月 截止时间：2026年06月"
    #       跨行 "起始时间：2024年01月" / "截止时间：2026年06月"
    #       多对 "起始...截止...起始...截止..."
    if not periods:
        lines = text.split('\n')
        pending_starts = []  # 未配对的起始时间
        for i, line in enumerate(lines):
            # 搜索起始时间（在关键词之后找日期）
            for kw in ['起始', '开始', '起期', '起止']:
                idx = line.find(kw)
                if idx >= 0:
                    after = line[idx + len(kw):]
                    m = re.search(r'(\d{4})\s*[-./年]\s*(\d{1,2})', after)
                    if m:
                        y, mo = int(m.group(1)), int(m.group(2))
                        if 1900 <= y <= 2100 and 1 <= mo <= 12:
                            pending_starts.append((y, mo))
                    break

            # 搜索截止时间（在关键词之后找日期，避免误匹配起始日期）
            for kw in ['截止', '结束', '止期', '终止']:
                idx = line.find(kw)
                if idx >= 0:
                    after = line[idx + len(kw):]
                    m = re.search(r'(\d{4})\s*[-./年]\s*(\d{1,2})', after)
                    if m:
                        y, mo = int(m.group(1)), int(m.group(2))
                        if 1900 <= y <= 2100 and 1 <= mo <= 12:
                            if pending_starts:
                                s = pending_starts.pop()
                                p = make_period(s[0], s[1], y, mo)
                                if p:
                                    periods.append(p)
                    break

    return periods


def extract_period_from_yearly_table(text):
    """
    从"城镇职工基本医疗保险参保缴费证明"格式中提取时间段

    OCR文本格式（3列布局）：
        缴费年度 缴费月份 缴费年度 缴费月份 缴费年度 缴费月份
        2000 0（月） 2010 0（月） 2020 0（月）
        2023 11（月） 2024 12（月） 2025 12（月）
        ...

    解析策略：
    1. 先用"缴费年度"关键词定位表格区域（避免身份证号等长字符串干扰）
    2. 在表格区域内匹配 (年份, 月份数) 对（必须有"月"字标识）
    3. 过滤月份数>0的年份（=0表示当年未参保）
    4. 合并连续年份为一段
    5. 多年段起止为 (年份-(13-月份数), 年份-月份数)，如10月→3月起12月止
    6. v1.1.35: 若所有参保月数集中在同一年度（仅单一年度），该年段改为
       (年份-02, 年份-(月份数+1))，如6月→2月起7月止；跨多个年度时不适用

    Returns:
        list of (start_ym, end_ym) 元组列表，按开始时间升序
        找不到有效记录返回 []
    """
    # 定位表格区域：从"缴费年度"或"缴费月份"后开始
    table_start = -1
    for kw in ['缴费年度', '缴费月份']:
        idx = text.find(kw)
        if idx >= 0 and (table_start < 0 or idx < table_start):
            table_start = idx

    if table_start < 0:
        return []

    table_text = text[table_start:]

    # 收集 (year, months) 对
    pairs = []
    pattern = r'(\d{4})[\s\u3000]+(\d{1,2})\s*[（(]?\s*月\s*[）)]?'
    for m in re.finditer(pattern, table_text):
        year = int(m.group(1))
        months = int(m.group(2))
        if 1900 <= year <= 2100 and 0 <= months <= 12 and months > 0:
            pairs.append((year, months))

    if not pairs:
        return []

    # 去重：同年保留最大月数（容错）
    by_year = {}
    for year, months in pairs:
        if year not in by_year or months > by_year[year]:
            by_year[year] = months

    sorted_pairs = sorted(by_year.items())  # [(year, months), ...]

    # v1.1.35 新规则（单年段 02~em+1）仅当所有参保月数集中在同一年度时生效；
    # 参保月数横跨多个年度时不触发，单年段回归旧规则 01~em
    single_year_only = (len(sorted_pairs) == 1)

    # 合并连续年份为段，再根据段内位置计算起止月
    merged = []
    seg_first_y, seg_first_m = sorted_pairs[0]
    seg_last_y, seg_last_m = sorted_pairs[0]

    for i in range(1, len(sorted_pairs)):
        y, m = sorted_pairs[i]
        if y == seg_last_y + 1:
            seg_last_y = y
            seg_last_m = m
        else:
            # 结束当前段
            if seg_first_y == seg_last_y:
                if single_year_only:
                    # v1.1.35 单年段新规则：起 2 月、止 em+1 月（em=12 时归为次年 1 月）
                    end_year = seg_last_y + (1 if seg_last_m == 12 else 0)
                    end_month = 1 if seg_last_m == 12 else seg_last_m + 1
                    merged.append((f'{seg_first_y:04d}-02', f'{end_year:04d}-{end_month:02d}'))
                else:
                    # 多年度记录中的单年段：旧规则 01~em
                    merged.append((f'{seg_first_y:04d}-01', f'{seg_last_y:04d}-{seg_last_m:02d}'))
            else:
                # 多年段：首年 (13-月数) 起，末年 月数 止
                merged.append((f'{seg_first_y:04d}-{13 - seg_first_m:02d}',
                               f'{seg_last_y:04d}-{seg_last_m:02d}'))
            seg_first_y, seg_first_m = y, m
            seg_last_y, seg_last_m = y, m

    # 最后一段
    if seg_first_y == seg_last_y:
        if single_year_only:
            # v1.1.35 单年段新规则：起 2 月、止 em+1 月
            end_year = seg_last_y + (1 if seg_last_m == 12 else 0)
            end_month = 1 if seg_last_m == 12 else seg_last_m + 1
            merged.append((f'{seg_first_y:04d}-02', f'{end_year:04d}-{end_month:02d}'))
        else:
            # 多年度记录中的单年段：旧规则 01~em
            merged.append((f'{seg_first_y:04d}-01', f'{seg_last_y:04d}-{seg_last_m:02d}'))
    else:
        merged.append((f'{seg_first_y:04d}-{13 - seg_first_m:02d}',
                       f'{seg_last_y:04d}-{seg_last_m:02d}'))

    # 只保留最近连续段（结束时间最晚的一段）
    return _keep_latest_segment(merged)


def extract_period_from_yearly_table_items(items):
    """
    从OCR items中提取年度表格型参保时间段（用于"城镇职工基本医疗保险参保缴费证明"）

    表格结构：3对 (缴费年度, 缴费月份) 列，共 6 列
    通过 x 坐标聚类识别 6 列，每列内按 y 顺序排列后配对
    相比纯文本解析，可以正确处理"年份"和"月份"在不同 y_bucket 的情况

    Args:
        items: list of dict, each with 'text', 'x', 'y', 'score'

    Returns:
        list of (start_ym, end_ym) 元组列表，按开始时间升序
        找不到有效记录返回 []
    """
    if not items:
        return []

    # Step 1: 找到 6 个表头（缴费年度/缴费月份 x 3）
    headers = [it for it in items
               if '缴费年度' in it['text'] or '缴费月份' in it['text']]
    if len(headers) < 6:
        return []

    # 按 x 排序：缴费年度, 缴费月份, 缴费年度, 缴费月份, 缴费年度, 缴费月份
    headers.sort(key=lambda h: h['x'])
    col_centers = [h['x'] for h in headers]
    n_cols = len(col_centers)

    # Step 2: 计算列边界（相邻表头的中点）
    col_bounds = []
    for i, cx in enumerate(col_centers):
        left = -float('inf') if i == 0 else (col_centers[i - 1] + cx) / 2
        right = float('inf') if i == n_cols - 1 else (cx + col_centers[i + 1]) / 2
        col_bounds.append((left, right))

    # Step 3: 仅保留表头以下的 items
    header_y = max(h['y'] for h in headers)
    table_items = [it for it in items if it['y'] > header_y + 10]

    # Step 4: 将每个 item 分配到对应的列
    col_items = [[] for _ in col_centers]
    for it in table_items:
        for i, (left, right) in enumerate(col_bounds):
            if left <= it['x'] < right:
                col_items[i].append(it)
                break

    # Step 5: 解析年份和月份
    def parse_year(text):
        m = re.match(r'^\s*(\d{4})\s*$', text)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                return y
        return None

    def parse_month(text):
        # 匹配 N月 / N（月）/ N(月) 等
        m = re.search(r'(\d{1,2})\s*[（(]?\s*月\s*[）)]?', text)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 12:
                return val
        return None

    # Step 6: 对每对 (year_col, month_col) 按 y 顺序配对
    year_month_pairs = []
    for col_idx in range(0, n_cols, 2):
        yc_sorted = sorted(col_items[col_idx], key=lambda it: it['y'])
        mc_sorted = sorted(col_items[col_idx + 1], key=lambda it: it['y'])

        years = [y for it in yc_sorted
                 for y in [parse_year(it['text'])] if y is not None]
        months = [m for it in mc_sorted
                  for m in [parse_month(it['text'])] if m is not None]

        for y, m in zip(years, months):
            if m > 0:
                year_month_pairs.append((y, m))

    if not year_month_pairs:
        return []

    # Step 7: 同年去重（保留最大月数，容错）
    by_year = {}
    for y, m in year_month_pairs:
        if y not in by_year or m > by_year[y]:
            by_year[y] = m

    sorted_pairs = sorted(by_year.items())

    # Step 8: 合并连续年份为一段
    segments = []
    cur_start_y = sorted_pairs[0][0]
    cur_start_m = sorted_pairs[0][1]  # 首年月数（用于计算起始月）
    cur_end_y = cur_start_y
    cur_end_m = sorted_pairs[0][1]

    for i in range(1, len(sorted_pairs)):
        y, m = sorted_pairs[i]
        if y == cur_end_y + 1:
            cur_end_y = y
            cur_end_m = m
        else:
            segments.append((cur_start_y, cur_end_y, cur_end_m, cur_start_m))
            cur_start_y = y
            cur_start_m = m
            cur_end_y = y
            cur_end_m = m
    segments.append((cur_start_y, cur_end_y, cur_end_m, cur_start_m))

    # Step 9: 转换为 (start_ym, end_ym)
    # v1.1.35 新规则（单年段 02~em+1）仅当所有参保月数集中在同一年度时生效；
    # 参保月数横跨多个年度时不触发，单年段回归旧规则 01~em
    # 多年段：首年 (13-月数) 起（如10月→3月起），末年 月数 止（如6月→6月止）
    single_year_only = (len(sorted_pairs) == 1)
    result = []
    for sy, ey, em, sm in segments:
        if sy == ey:
            if single_year_only:
                # 单年且为唯一年度：起 2 月、止 em+1 月（em=12 时归为次年 1 月）
                end_year = ey + (1 if em == 12 else 0)
                end_month = 1 if em == 12 else em + 1
                result.append((f'{sy:04d}-02', f'{end_year:04d}-{end_month:02d}'))
            else:
                # 多年度记录中的单年段：旧规则 01~em
                result.append((f'{sy:04d}-01', f'{ey:04d}-{em:02d}'))
        else:
            result.append((f'{sy:04d}-{13 - sm:02d}', f'{ey:04d}-{em:02d}'))

    # 只保留最近连续段（结束时间最晚的一段）
    return _keep_latest_segment(result)


def get_full_period(text):
    """
    从OCR文本中提取完整参保时间段

    只保留最近连续缴费段（不连续的旧时间段忽略）。
    优先尝试"起始-截止"型时间段（extract_periods），
    若结果为空，尝试"年份+月数"表格型（城镇职工基本医疗保险证明）。

    Returns:
        tuple: (start_ym, end_ym) 如 ('2024-01', '2026-04')，无法提取返回None
    """
    periods = extract_periods(text)
    if periods:
        # 只保留最近连续段
        latest = _keep_latest_segment(periods)
        return latest[0][0], latest[0][1]

    # 回退：年度+月数表格（纯文本方式，已内置过滤）
    table_periods = extract_period_from_yearly_table(text)
    if table_periods:
        return table_periods[0][0], table_periods[0][1]

    return None


def get_full_period_from_items(items):
    """
    从OCR items中提取完整参保时间段（更稳健的 items 方式）

    只保留最近连续缴费段（不连续的旧时间段忽略）。
    优先尝试"起始-截止"型时间段（先用纯文本匹配），
    若结果为空，使用"年份+月数"表格型（用 x/y 坐标配对）。

    Returns:
        tuple: (start_ym, end_ym) 如 ('2024-01', '2026-04')，无法提取返回None
    """
    # 构造 raw_text 供文本匹配使用
    from collections import defaultdict
    line_groups = defaultdict(list)
    for it in items:
        bucket = round(it['y'] / 15)
        line_groups[bucket].append(it)

    lines = []
    for bucket in sorted(line_groups.keys()):
        sorted_items = sorted(line_groups[bucket], key=lambda it: it['x'])
        lines.append(' '.join(it['text'] for it in sorted_items))
    raw_text = '\n'.join(lines)

    # 优先尝试文本中的"起始-截止"型
    text_periods = extract_periods(raw_text)
    if text_periods:
        # 只保留最近连续段
        latest = _keep_latest_segment(text_periods)
        return latest[0][0], latest[0][1]

    # 回退：年度+月数表格（items 方式，已内置过滤）
    table_periods = extract_period_from_yearly_table_items(items)
    if table_periods:
        return table_periods[0][0], table_periods[0][1]

    return None


# ============ 缴费单位提取 ============
def _strip_parentheses(text):
    """
    去掉公司名称中的括号及括号内内容，只保留括号外的名称
    例如: "鲁岳企业服务有限公司（济南分公司）" -> "鲁岳企业服务有限公司"
    """
    if not text:
        return text
    result = re.sub(r'[（(][^（）()]*[）)]', '', text)
    return result.strip()


def extract_company_name(text):
    """
    从OCR文本中提取缴费单位/参保单位名称

    策略：查找"现缴费单位"/"缴费单位"或"参保单位"关键词，提取后面的单位名称。
    v1.1.34: "现缴费单位"关键词优先；提取结果去掉括号及括号内内容（只取括号外公司名）。

    Returns:
        str: 单位名称，未找到返回空字符串
    """
    lines = text.split('\n')
    # 长关键词优先，避免"缴费单位名称"被"缴费单位"误匹配；"现缴费单位"最优先
    keywords = ['现缴费单位名称', '现缴费单位', '缴费单位名称', '参保单位名称',
                '缴费单位', '参保单位', '用人单位', '单位名称', '单位:', '单位：']

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
                    return _strip_parentheses(m.group(1))
                # 如果当前行后面内容不够，看下一行
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    m = re.match(r'([\u4e00-\u9fa5\w\(\)（）\u00b7·]{2,40})', next_line)
                    if m:
                        return _strip_parentheses(m.group(1))

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


def parse_ocr_result_from_image(image_path):
    """
    对图片进行OCR并解析（使用 items + x/y 坐标，更稳健）

    相比 parse_ocr_result(text)，时间表解析使用 x 坐标分列配对，
    可以正确处理"年份"和"月份"在不同 y_bucket 的情况
    （如城镇职工基本医疗保险参保缴费证明：3对列布局，年份和月份在视觉上
    相邻但 y 坐标差 3-5 像素，刚好跨越 round(y/15) 边界）。

    Args:
        image_path: 图片文件路径

    Returns:
        dict: 与 parse_ocr_result 相同结构
    """
    from modules.insurance.core.ocr_engine import ocr_image

    items = ocr_image(image_path)
    if not items:
        return {
            'insurance_type': None,
            'name': '',
            'idcard': '',
            'period': None,
            'company_name': '',
            'raw_text': '',
        }

    # 构造 raw_text 供其他字段提取使用（与 ocr_to_text 一致）
    from collections import defaultdict
    line_groups = defaultdict(list)
    for it in items:
        bucket = round(it['y'] / 15)
        line_groups[bucket].append(it)

    lines = []
    for bucket in sorted(line_groups.keys()):
        sorted_items = sorted(line_groups[bucket], key=lambda it: it['x'])
        lines.append(' '.join(it['text'] for it in sorted_items))
    raw_text = '\n'.join(lines)

    # 用 items 提取时间表（更稳健）
    period = get_full_period_from_items(items)

    return {
        'insurance_type': detect_insurance_type(raw_text),
        'name': extract_name(raw_text),
        'idcard': extract_idcard(raw_text),
        'period': period,
        'company_name': extract_company_name(raw_text),
        'raw_text': raw_text,
    }


def group_by_person(records):
    """
    将多条OCR解析记录按人员分组（姓名+身份证号）

    v1.1.40: 支持花名册重名场景——同一姓名下存在多个不同身份证号时，
    按身份证号拆分为不同人员（与花名册"序号+姓名区分"规则配合）。

    Args:
        records: list of parse_ocr_result()返回的dict

    Returns:
        list of dict: 每个元素代表一个人，包含：
            'name': 姓名
            'idcard': 身份证号
            'insurances': {险种: (start, end)}
    """
    # 第一遍：按姓名收集原始记录（不直接合并，保留身份证号信息）
    by_name = {}  # name -> list of records
    for rec in records:
        if not rec['name'] or not rec['insurance_type']:
            continue
        by_name.setdefault(rec['name'], []).append(rec)

    # 第二遍：同一姓名下按身份证号拆分
    persons = {}
    for name, recs in by_name.items():
        # 收集该姓名下所有非空身份证号（去重，保持出现顺序）
        distinct_ids = []
        for rec in recs:
            if rec['idcard'] and rec['idcard'] not in distinct_ids:
                distinct_ids.append(rec['idcard'])

        if len(distinct_ids) <= 1:
            # 单一人员（或身份证号全为空/唯一）：按姓名合并为一人
            persons[name] = {
                'name': name,
                'idcard': distinct_ids[0] if distinct_ids else '',
                'insurances': {},
                '_recs': recs,
            }
        else:
            # 重名多人：按身份证号拆分为不同人员
            for rec in recs:
                if rec['idcard']:
                    key = (name, rec['idcard'])
                else:
                    # 身份证号缺失的记录归入该姓名下第一个出现的身份证号
                    # （OCR缺失时无法进一步区分，按出现顺序归档）
                    key = (name, distinct_ids[0])
                if key not in persons:
                    persons[key] = {
                        'name': name,
                        'idcard': rec['idcard'] if rec['idcard'] else distinct_ids[0],
                        'insurances': {},
                        '_recs': [],
                    }
                persons[key]['_recs'].append(rec)

    # 汇总每个人的险种时间段
    for p in persons.values():
        for rec in p.pop('_recs'):
            ins_type = rec['insurance_type']
            period = rec['period']
            if period:
                if ins_type not in p['insurances']:
                    p['insurances'][ins_type] = []
                p['insurances'][ins_type].append(period)

    # 对每个人的每个险种：合并所有记录的时间段，只保留最近连续段
    result = []
    for p in persons.values():
        filtered = {}
        for ins_type, periods in p['insurances'].items():
            latest = _keep_latest_segment(periods)
            if latest:
                filtered[ins_type] = latest[0]
        p['insurances'] = filtered
        result.append(p)

    return result
