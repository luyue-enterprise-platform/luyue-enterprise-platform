# -*- coding: utf-8 -*-
"""省份模板引擎（多省份参保证明识别扩展 · 第一阶段）

设计要点（详见架构方案）：
1. 模板是数据不是代码：内置模板（Python 定义）+ 外置模板（JSON 文件热加载），
   省份列表由已加载模板动态生成，前端下拉永不硬编码。
2. 省份完全以用户手动选择为准（必选项）：系统不从文件内容推断、也不校验
   文件与所选省份是否相符——选定省份即路由到该省模板，按原有规则识别统计。
   锚点（anchors）仅用于同省多版式时的"版式择优"（打分排序）与日志记录，
   不产生任何拦截。
3. 陕西为第一份内置模板：解析实现直接委托 data_parser 现有函数，
   对外行为与改造前完全一致（选陕西 = 原有识别逻辑）。
4. 省份仅用于读取规则匹配：路由到该省模板解析后返回 data_parser 原有结构，
   不向解析记录/任务结果添加任何额外字段（province_code 等），
   任务内部仅以 _province_code 记住路由省份供补充上传沿用。

外置模板格式（JSON，放 DATA_DIR/insurance_templates/*.json）：
{
  "template_id": "410000_si_2026",
  "province_code": "410000",
  "province_name": "河南",
  "version": 1,
  "anchors": [{"text": "河南省", "weight": 3}],
  "min_match_score": 3,
  "province_hints": ["河南"],
  "parser": "regex",
  "fields": {
    "name":          {"regex": "姓名[：:]?\\s*(\\S{2,4})", "group": 1},
    "idcard":        {"regex": "(\\d{17}[\\dXx])", "group": 1},
    "company_name":  {"regex": "缴费单位[：:]?\\s*(\\S+)", "group": 1},
    "insurance_type": {"patterns": {"养老保险": ["养老"], "医疗保险": ["医疗"]}}
  },
  "period": {"regex": "(\\d{4})[年.\\-/](\\d{1,2})月?\\s*[-至~]\\s*(\\d{4})[年.\\-/](\\d{1,2})月?"}
}
"""
import json
import os
import re
import sys
import logging

logger = logging.getLogger('insurance.template')

# ============ 路径（与 blueprint 的 DATA_DIR 约定一致） ============
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    _DATA_DIR = os.path.dirname(sys.executable)
else:
    _DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 外置模板热加载目录（EXE 同目录 / 项目根目录下）
EXTERNAL_TEMPLATE_DIR = os.path.join(_DATA_DIR, 'insurance_templates')

# 默认省份：陕西（保证存量用户行为零变化）
DEFAULT_PROVINCE = '610000'


class Template:
    """一个省份版式的解析模板"""

    def __init__(self, template_id, province_code, province_name,
                 version=1, anchors=None, min_match_score=1,
                 province_hints=None, parser='builtin_shaanxi',
                 fields=None, period=None, source='builtin', path=None,
                 multi_insurance=None, actual_payment_end=False):
        self.template_id = template_id
        self.province_code = str(province_code)
        self.province_name = province_name
        self.version = version
        # anchors: [{'text': 锚点词, 'weight': 权重}]
        self.anchors = anchors or []
        self.min_match_score = min_match_score
        self.province_hints = province_hints or []
        self.parser = parser
        self.fields = fields or {}
        self.period = period or {}
        self.source = source          # builtin | external
        self.path = path
        # v2.6.0 一单多险（江苏/浙江）：一张证明单同时覆盖多个险种，
        # 解析后由 _expand_multi_insurance 复制为多条记录，交 group_by_person 归类。
        # None/[] = 单险种行为（与改造前一致，陕西即此）。
        self.multi_insurance = multi_insurance or []
        # v2.6.0 时间段口径：True = 取明细实际缴费月终点（江苏/浙江），
        # False = 沿用原逻辑（陕西，零行为变化）。
        self.actual_payment_end = bool(actual_payment_end)

    # ---------- 锚点打分 ----------
    def anchor_score(self, text):
        """锚点命中得分（同省多版式择优排序用，不做拦截）"""
        if not text:
            return 0
        score = 0
        for a in self.anchors:
            if a.get('text') and a['text'] in text:
                score += int(a.get('weight', 1))
        return score

    def matches(self, text):
        return self.anchor_score(text) >= self.min_match_score

    def has_province_hint(self, text):
        """省份提示词命中（非阻断告警用）"""
        if not text:
            return False
        return any(h in text for h in self.province_hints)

    # ---------- 解析 ----------
    def parse(self, text, items=None):
        """按模板解析 OCR 结果，返回与 parse_ocr_result 相同结构的 dict"""
        if self.parser == 'builtin_shaanxi':
            return _builtin_shaanxi_parse(text, items, self)
        if self.parser == 'regex':
            return _regex_parse(self, text)
        raise ValueError('未知解析器: %s' % self.parser)

    def parse_multi(self, text, items=None):
        """解析并返回记录列表（一单多险展开后的结果）

        单险种模板（multi_insurance 为空）返回单元素列表，行为与 parse 一致；
        一单多险模板（江苏/浙江）返回多条记录，每条 insurance_type 不同，
        其余字段（姓名/身份证/单位/时间段）相同。
        """
        base = self.parse(text, items)
        if not self.multi_insurance:
            return [base]
        return _expand_multi_insurance(base, self.multi_insurance)

    def info(self):
        return {
            'template_id': self.template_id,
            'province_code': self.province_code,
            'province_name': self.province_name,
            'version': self.version,
            'source': self.source,
        }


# ============ 内置模板：陕西（行为与改造前完全一致） ============
# 锚点仅用于版式择优排序与日志（省份以用户手动选择为准，不做内容校验拦截）。
BUILTIN_TEMPLATES = [
    Template(
        template_id='610000_si_builtin',
        province_code='610000',
        province_name='陕西',
        version=1,
        anchors=[
            {'text': '参保', 'weight': 1},
            {'text': '缴费', 'weight': 1},
            {'text': '保险', 'weight': 1},
            {'text': '权益记录', 'weight': 1},
            {'text': '社会保险', 'weight': 1},
        ],
        min_match_score=1,
        province_hints=[],
        parser='builtin_shaanxi',
    ),
    # v2.6.0 江苏：省社会保险权益记录单（参保人员）——养老/工伤/失业三险合并一张表，
    # 年月明细行为主 + 抬头"出具证明前N个月缴费情况（YYYYMM-YYYYMM）"声明区间。
    # 实测结论：现有解析实现（姓名/身份证/单位/表格型时间段）可直接命中，
    # 故 parser 复用 builtin_shaanxi；差异仅在时间段口径（见 _pick_period 说明）
    # 与三险拆分（见 _expand_multi_insurance）。
    Template(
        template_id='320000_si_2026',
        province_code='320000',
        province_name='江苏',
        version=1,
        anchors=[
            {'text': '江苏智慧人社', 'weight': 3},
            {'text': '江苏省社会保险权益记录单', 'weight': 3},
            {'text': '现参保单位全称', 'weight': 2},
            {'text': '出具证明前', 'weight': 1},
        ],
        min_match_score=1,
        province_hints=['江苏'],
        parser='builtin_shaanxi',
        # 明细表：首列"年"、次列"月"，逐行成对（专用解析器，见 _parse_year_month_columns）
        period={'table_parser': 'year_month_columns'},
        multi_insurance=['养老保险', '工伤保险', '失业保险'],
        actual_payment_end=True,
    ),
    # v2.6.0 浙江：省社会保险参保证明（个人专用）——养老/工伤/失业三险合并，
    # 明细行含"单位编号 + 参保地 + 缴费基数 + 个人缴费 + 缴费状况"，
    # 抬行为"最近24个月缴费情况(YYYY年MM月-YYYY年MM月)"。
    # 同江苏：主字段解析复用现有实现，差异在多险拆分与年份月份为 6 位连写
    # （YYYYMM）时的时间段兜底。
    Template(
        template_id='330000_si_2026',
        province_code='330000',
        province_name='浙江',
        version=1,
        anchors=[
            {'text': '浙江省社会保险参保证明', 'weight': 3},
            {'text': '浙江省社会保险', 'weight': 2},
            {'text': '单位编号', 'weight': 1},
            {'text': '缴费状况', 'weight': 1},
        ],
        min_match_score=1,
        province_hints=['浙江'],
        parser='builtin_shaanxi',
        # 浙江明细同为"年/月"两列版式（另含单位编号/参保地列），
        # 共用 year_month_columns 解析器；缴费状况为"已到账/未到账"不影响取月。
        period={'table_parser': 'year_month_columns'},
        multi_insurance=['养老保险', '工伤保险', '失业保险'],
        actual_payment_end=True,
    ),
]


def _builtin_shaanxi_parse(text, items=None, tpl=None):
    """陕西内置模板：委托 data_parser 现有实现（零行为变化）

    v2.6.0：新增 tpl 参数供江苏/浙江复用——它们的主字段（姓名/身份证/单位）
    与陕西逻辑一致，差异仅在时间段口径（取实际缴费月终点，见 _pick_period）
    与"一单多险"展开（由 Template.parse_multi 处理）。tpl 为 None 时
    行为与改造前完全一致（存量调用零变化）。
    """
    from modules.insurance.core import data_parser as dp

    result = {
        'insurance_type': dp.detect_insurance_type(text),
        'name': dp.extract_name(text),
        'idcard': dp.extract_idcard(text),
        'company_name': dp.extract_company_name(text),
        'raw_text': text,
    }
    if items is not None:
        result['period'] = _pick_period(dp, text, items, tpl)
    else:
        result['period'] = _pick_period(dp, text, None, tpl)
    return result


def _pick_period(dp, text, items, tpl):
    """时间段取值（v2.6.0 省份差异化）

    - 陕西（tpl 为 None 或未声明 actual_payment_end）：沿用原有实现，
      即优先"清空中断段后的文本解析"，items 存在时用"年份+月数"表格型兜底。
    - 江苏/浙江（actual_payment_end=True）：明细行才是实际缴费记录，
      抬头"出具证明前N个月缴费情况（YYYYMM-YYYYMM）"这类声明区间的终点
      常比实际缴费月多 1 个月（打印当月未入账）。而 dp.get_full_period_from_items
      会先用文本正则命中该声明区间、永不落到明细表格；陕西医保版式的
      extract_period_from_yearly_table_items 又要求"6 个缴费年度/缴费月份表头"，
      对江苏"年/月两列"版式不适用。故此处走专用明细行解析（按 年/月 列配对）。
    """
    if tpl is not None and getattr(tpl, 'actual_payment_end', False):
        if items is not None:
            parser = (tpl.period or {}).get('table_parser')
            if parser == 'year_month_columns':
                got = _parse_year_month_columns(items, text)
                if got:
                    return got
        # 回退：老逻辑（先文本正则，再陕西医保表格型）
        return dp.get_full_period_from_items(items) if items is not None else dp.get_full_period(text)

    if items is not None:
        return dp.get_full_period_from_items(items)
    return dp.get_full_period(text)


def _parse_year_month_columns(items, text=None):
    """江苏/浙江版式：明细表首列为"年"，次列为"月"，逐行成对

    定位方式（不依赖固定像素）：先找表头的"年""月"两个字块拿到列中心，
    再取所有位于两列内的 4 位年份 / 1-2 位月份，按 y 邻近配对成 (年,月)，
    去重排序后取最小月为起点、最大月为终点（只保留最近连续段）。

    该函数只在江苏/浙江模板（period.table_parser == 'year_month_columns'）
    下被调用，不影响陕西等既有省份。
    """
    if not items:
        return None

    # Step 1: 定位"年""月"表头（同一行、相近 y，x 递增）
    year_hdr = [it for it in items if it['text'].strip() == '年']
    month_hdr = [it for it in items if it['text'].strip() == '月']
    if not year_hdr or not month_hdr:
        return None

    pairs_hdr = []
    for yh in year_hdr:
        for mh in month_hdr:
            if abs(yh['y'] - mh['y']) < 20 and 0 < (mh['x'] - yh['x']) < 120:
                pairs_hdr.append((yh, mh))
    if not pairs_hdr:
        return None
    # 取最靠上的那组表头（表格区起始）
    yh, mh = min(pairs_hdr, key=lambda p: p[0]['y'])
    col_year_x, col_month_x = yh['x'], mh['x']
    header_y = max(yh['y'], mh['y'])

    # Step 2: 年份列 / 月份列候选（表头以下，x 落在列中心附近）
    x_tol = 45
    years = [it for it in items
             if it['y'] > header_y + 8
             and abs(it['x'] - col_year_x) <= x_tol
             and re.fullmatch(r'20\d{2}', it['text'].strip())]
    months = [it for it in items
              if it['y'] > header_y + 8
              and abs(it['x'] - col_month_x) <= x_tol
              and re.fullmatch(r'\d{1,2}', it['text'].strip())]
    if not years:
        return None

    # Step 3: 按 y 邻近把月份配到年份行上
    ym = []
    for yv in years:
        best = None
        for mv in months:
            d = abs(mv['y'] - yv['y'])
            if d < 20 and (best is None or d < best[0]):
                best = (d, int(mv['text']))
        if best and 1 <= best[1] <= 12:
            ym.append((int(yv['text']), best[1]))
    if not ym:
        return None

    # Step 4: 去重 + 排序
    uniq = sorted(set(ym))
    if not uniq:
        return None

    # Step 5: 起点校正（v2.6.0 江苏/浙江）
    # 终点（最近缴费月）取明细最大月，OCR 可靠；起点受首行"0/1 混淆"影响
    # （09 误读 10）易漂移，且票面"前N个月"声明含打印当月、比明细多 1 行，
    # 不能直接用作长度。改用**明细行数**（单位名称列/年份列块数）作为实际
    # 缴费月数 N，起点 = 终点 -(N-1) 月，绕开首行误读。
    n_rows = _count_detail_rows(items, col_year_x)
    ey, em = uniq[-1]
    if n_rows >= 2:
        total = ey * 12 + em - (n_rows - 1)
        sy, sm = divmod(total, 12)
        if sm == 0:
            sy, sm = sy - 1, 12
        start = '%04d-%02d' % (sy, sm)
        end = '%04d-%02d' % (ey, em)
        # 合理性校验：推算起点不得晚于明细最早月（否则说明行数异常，回退）
        if idx_of(sy, sm) <= idx_of(uniq[0][0], uniq[0][1]):
            return (start, end)

    uniq = _fix_month_continuity(uniq)
    if not uniq:
        return None
    return ('%04d-%02d' % uniq[0], '%04d-%02d' % uniq[-1])


def idx_of(y, m):
    return y * 12 + m


def _count_detail_rows(items, col_year_x=None):
    """统计明细行数（江苏/浙江）：以"单位全称"列文本块为准

    江苏/浙江的单位全称列因列宽限制会折行显示（"…有限公" + "司" 两块），
    但每行必有一块以"有限公"/"公司"/"集团"等开头或结尾的长文本。
    OCR 对这类长文本块最稳定（不涉及易混的 0/1 单字），故以它计行数。

    兜底顺序：
    1. 公司名主体块（以"…公司""…有限公""…集团"等形态出现）计数；
    2. 年份列块数（可能有漏读，仅兜底）。
    """
    # 明细行公司名主体：多为"XX公司"或折行为"XX有限公"，统一取含公司特征词的块
    tokens = ('有限公司', '有限责任', '有限公', '公司', '集团', '事务所', '合作社')
    comp_rows = [it for it in items
                 if any(t in it['text'] for t in tokens)
                 and len(it['text'].strip()) >= 4]
    # 关键：排除表格区上方的"现参保单位全称"值行——它位于表头之上，
    # 只统计表头（"年""月"那行）y 以下的块，否则会多算 1 行。
    hdr_y = None
    for it in items:
        if it['text'].strip() == '年':
            hdr_y = it['y'] if hdr_y is None else min(hdr_y, it['y'])
    if hdr_y is not None:
        comp_rows = [it for it in comp_rows if it['y'] > hdr_y]
    if len(comp_rows) >= 3:
        return len(comp_rows)

    if col_year_x is not None:
        yrs = [it for it in items
               if abs(it['x'] - col_year_x) <= 45
               and re.fullmatch(r'20\d{2}', it['text'].strip())]
        if yrs:
            return len(yrs)
    return 0
    """从票面声明区提取"前 N 个月 / 最近 N 个月"的 N（江苏/浙江版式确定文本）

    江苏："出具证明前37个月缴费情况（202309-202609）"
    浙江："出具证明前24个月缴费情况（2023年02月-2025年01月）"
          / "最近24个月缴费情况(2021年08月-2023年07月)"
    取不到返回 None（不影响其他省份）。
    """
    if not text:
        return None
    m = re.search(r'(?:前|最近)\s*(\d{1,3})\s*个月', text)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 600:
                return n
        except ValueError:
            return None
    return None


def _fix_month_continuity(ym_list):
    """月份连续性校正：把疑似 0/1 混淆的月修正为连续序列

    输入 [(年,月), ...]（已去重排序）。逐对检查相邻月份序号：
    - 正常递增 1 → 保留
    - 出现 9 个月以上的"跳跃"（如 09→10 实际应为 09→10 正常，
      但 12→10 这类逆序或跳 2 个月以上）→ 判定后者为 0/1 混淆，
      按前者 +1 回推（跨年时进位）。
    - 逆序（后者序号 ≤ 前者）→ 同样按前者 +1 回推。
    仅在"异常"时修正，正常序列不动，避免过度干预。
    """
    if len(ym_list) < 2:
        return ym_list

    def idx(y, m):
        return y * 12 + m

    out = [ym_list[0]]
    for (py, pm), (cy, cm) in zip(ym_list, ym_list[1:]):
        expect = idx(py, pm) + 1
        cur = idx(cy, cm)
        if cur == expect:
            out.append((cy, cm))
        else:
            # 异常：按连续假设回推（修正当前月）
            ny, nm = divmod(expect, 12)
            if nm == 0:
                ny, nm = ny - 1, 12
            out.append((ny, nm))
    # 去重（修正后可能与后续重复）
    dedup = []
    seen = set()
    for item in out:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


def _expand_multi_insurance(base, insurance_types):
    """一单多险展开（v2.6.0 江苏/浙江）

    一张证明单同时覆盖养老/工伤/失业三险时，复制为多条记录，
    每条 insurance_type 取顺序表中的险种名，其余字段保持一致。
    这样 group_by_person 会自然把三条记录归到同一人的三个险种下，
    无需改动聚合逻辑。
    """
    records = []
    for ins in insurance_types:
        rec = dict(base)
        rec['insurance_type'] = ins
        records.append(rec)
    return records


def _regex_parse(tpl, text):
    """外置模板通用规则解析（阶段一：基础字段 + 首个时间段，尽力而为）"""
    result = {
        'insurance_type': None,
        'name': '',
        'idcard': '',
        'company_name': '',
        'raw_text': text,
        'period': None,
    }
    for key in ('name', 'idcard', 'company_name'):
        cfg = tpl.fields.get(key)
        if not cfg:
            continue
        try:
            m = re.search(cfg.get('regex', ''), text)
            if m:
                grp = int(cfg.get('group', 1))
                result[key] = m.group(grp) if m.lastindex and grp <= m.lastindex else m.group(0)
        except re.error:
            continue

    # 险种：关键词 → 险种名映射，按文本出现顺序取第一个命中
    ins_cfg = tpl.fields.get('insurance_type') or {}
    for ins_name, keywords in ins_cfg.get('patterns', {}).items():
        if any(kw in text for kw in keywords):
            result['insurance_type'] = ins_name
            break

    # 时间段：4 分组正则 (起年, 起月, 止年, 止月)
    pcfg = tpl.period or {}
    preg = pcfg.get('regex')
    if preg:
        try:
            m = re.search(preg, text)
            if m and m.lastindex and m.lastindex >= 4:
                y1, m1, y2, m2 = (int(m.group(i)) for i in range(1, 5))
                result['period'] = ('%04d-%02d' % (y1, m1), '%04d-%02d' % (y2, m2))
        except (re.error, ValueError):
            pass
    return result


# ============ 模板注册表（内置 + 外置热加载） ============
_registry_cache = None          # {province_code: [Template, ...]}
_registry_mtime = None          # 外置目录快照（mtime+文件数），变更时自动重载


def _scan_external_dir():
    """扫描外置模板目录，返回 (mtime签名, [Template])"""
    if not os.path.isdir(EXTERNAL_TEMPLATE_DIR):
        return (0, [])
    sig = []
    templates = []
    for fn in sorted(os.listdir(EXTERNAL_TEMPLATE_DIR)):
        if not fn.lower().endswith('.json'):
            continue
        fp = os.path.join(EXTERNAL_TEMPLATE_DIR, fn)
        try:
            sig.append((fn, os.path.getmtime(fp), os.path.getsize(fp)))
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            tpl = Template(
                template_id=data.get('template_id', os.path.splitext(fn)[0]),
                province_code=data.get('province_code', ''),
                province_name=data.get('province_name', ''),
                version=int(data.get('version', 1)),
                anchors=data.get('anchors') or [],
                min_match_score=int(data.get('min_match_score', 1)),
                province_hints=data.get('province_hints') or [],
                parser=data.get('parser', 'regex'),
                fields=data.get('fields') or {},
                period=data.get('period') or {},
                source='external',
                path=fp,
            )
            if tpl.province_code and tpl.province_name:
                templates.append(tpl)
            else:
                logger.warning('外置模板缺少 province_code/province_name，已跳过: %s', fp)
        except Exception as e:
            logger.error('外置模板加载失败 %s: %s', fp, e)
    return (tuple(sig), templates)


def get_registry():
    """获取 {province_code: [Template]} 注册表（外置目录变更自动重载）"""
    global _registry_cache, _registry_mtime
    sig, external = _scan_external_dir()
    if _registry_cache is None or sig != _registry_mtime:
        registry = {}
        for tpl in BUILTIN_TEMPLATES + external:
            registry.setdefault(tpl.province_code, []).append(tpl)
        _registry_cache = registry
        _registry_mtime = sig
        logger.info('模板注册表已加载: %d 省 %d 模板（外置 %d 个）',
                    len(registry),
                    sum(len(v) for v in registry.values()), len(external))
    return _registry_cache


def get_provinces():
    """省份列表（数据驱动：有模板才出现）

    返回按省份码排序的列表，DEFAULT_PROVINCE 置顶。
    """
    registry = get_registry()
    provinces = []
    for code, tpls in registry.items():
        provinces.append({
            'province_code': code,
            'province_name': tpls[0].province_name,
            'template_count': len(tpls),
            'default': code == DEFAULT_PROVINCE,
        })
    provinces.sort(key=lambda p: (not p['default'], p['province_code']))
    return provinces


def match_template(text, province_code):
    """路由到指定省份的模板（省份完全以用户选择为准，不从内容推断）

    同省存在多套模板时按锚点打分择优（版式择优）；打分仅用于排序，
    任何内容都不产生拦截——选定省份即用该省模板按原有规则解析。

    返回 (Template|None, score, message)
    - 省份有模板 → (最高分模板, score, '')（全部 0 分时取第一套）
    - 省份无模板 → (None, 0, 该省份暂未支持...)
    """
    code = str(province_code or DEFAULT_PROVINCE)
    registry = get_registry()
    tpls = registry.get(code)
    if not tpls:
        available = '、'.join(p['province_name'] for p in get_provinces())
        return (None, 0, '该省份暂未支持（当前可用：%s），请联系公司获取模板' % available)

    best, best_score = tpls[0], -1
    for tpl in tpls:
        s = tpl.anchor_score(text)
        if s > best_score:
            best, best_score = tpl, s
    return (best, max(best_score, 0), '')


def parse_with_province(text, province_code, items=None):
    """按用户所选省份路由模板并解析（对外主入口）

    省份仅用于读取规则匹配：选定省份即路由到该省模板，按原有规则识别读取，
    不从内容推断、不做省份校验拦截。返回与 data_parser.parse_ocr_result
    完全相同的结构——不添加任何额外字段（template_id/anchor_score/province_code
    等均不出现在返回值中）。
    """
    tpl, score, err = match_template(text, province_code)
    if tpl is None:
        return {
            'insurance_type': None, 'name': '', 'idcard': '',
            'company_name': '', 'period': None, 'raw_text': text,
            'error': err,
        }
    return tpl.parse(text, items)
