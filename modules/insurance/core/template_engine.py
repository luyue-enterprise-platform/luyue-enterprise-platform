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
4. 省份关联与传递：解析记录逐条携带 province_code（关联到具体文件数据），
   任务内部状态 _province_code 与公开 result 均透传，保证传递至后续统计环节。

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
                 fields=None, period=None, source='builtin', path=None):
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
            return _builtin_shaanxi_parse(text, items)
        if self.parser == 'regex':
            return _regex_parse(self, text)
        raise ValueError('未知解析器: %s' % self.parser)

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
]


def _builtin_shaanxi_parse(text, items=None):
    """陕西内置模板：委托 data_parser 现有实现（零行为变化）"""
    from modules.insurance.core import data_parser as dp

    result = {
        'insurance_type': dp.detect_insurance_type(text),
        'name': dp.extract_name(text),
        'idcard': dp.extract_idcard(text),
        'company_name': dp.extract_company_name(text),
        'raw_text': text,
    }
    if items is not None:
        result['period'] = dp.get_full_period_from_items(items)
    else:
        result['period'] = dp.get_full_period(text)
    return result


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

    省份信息以用户手动选择为准：不从内容推断、不做省份校验拦截，
    按原有规则识别读取。返回与 data_parser.parse_ocr_result 相同结构的 dict，
    额外携带 province_code（关联文件数据）、template_id 与 anchor_score（日志用）。
    """
    tpl, score, err = match_template(text, province_code)
    if tpl is None:
        return {
            'insurance_type': None, 'name': '', 'idcard': '',
            'company_name': '', 'period': None, 'raw_text': text,
            'error': err,
            'template_id': None, 'anchor_score': score,
            'province_code': str(province_code or DEFAULT_PROVINCE),
        }
    result = tpl.parse(text, items)
    result['template_id'] = tpl.template_id
    result['anchor_score'] = score
    # 省份关联：逐条记录用户所选省份（以手动选择为准，不从内容推断）
    result['province_code'] = str(province_code or DEFAULT_PROVINCE)
    return result
