# -*- coding: utf-8 -*-
"""v1.1.51 测试套件

三项修改：
1. 异常图片修改后的页面交互优化（前端 app.js：保留面板展开状态+停留在被处理行，
   本套件以后端可测部分为主，前端逻辑靠 node --check + 关键字符串存在性验证）
2. 间断参保只计最近连续段：修复"中断信息明细"被误当参保时间段提取的根因
   （中断起止时间 201805-202104 与参保时间段格式一致，会短路年度表格解析路径）
3. 仅最近年度有参保记录：2026年6个月 → 2026-02~2026-07（v1.1.35 已有规则，
   本版本保证其在含中断信息时同样生效）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance.core.data_parser import (
    _strip_interruption_sections,
    _keep_latest_segment,
    extract_periods,
    get_full_period,
    get_full_period_from_items,
    group_by_person,
)


def _mk_item(text, x, y):
    return {'text': text, 'x': x, 'y': y, 'score': 0.99}


# ===== 刘伟民医保参保证明（用户截图）模拟数据 =====
MEDICAL_HEADER = '''城镇职工基本医疗保险参保缴费证明
姓名：刘伟民 身份证号：610502197505123456
缴费年度 缴费月份 缴费年度 缴费月份 缴费年度 缴费月份
2016 2（月） 2021 8（月） 2024 12（月）
2017 12（月） 2022 12（月） 2025 12（月）
2018 4（月） 2023 12（月） 2026 7（月）
'''


def _medical_items():
    """构造 6 列年度表格 + 中断信息明细 的 OCR items"""
    items = []
    xs = [100, 200, 400, 500, 700, 800]
    for i, x in enumerate(xs):
        items.append(_mk_item('缴费年度' if i % 2 == 0 else '缴费月份', x, 100))
    rows = [
        [('2016', 100), ('2（月）', 200), ('2021', 400), ('8（月）', 500), ('2024', 700), ('12（月）', 800)],
        [('2017', 100), ('12（月）', 200), ('2022', 400), ('12（月）', 500), ('2025', 700), ('12（月）', 800)],
        [('2018', 100), ('4（月）', 200), ('2023', 400), ('12（月）', 500), ('2026', 700), ('7（月）', 800)],
    ]
    for ri, row in enumerate(rows):
        for text, x in row:
            items.append(_mk_item(text, x, 150 + ri * 30))
    # 中断信息明细（文本行）
    items.append(_mk_item('中断信息明细', 100, 300))
    items.append(_mk_item('中断起止时间', 100, 330))
    items.append(_mk_item('201805-202104', 300, 330))
    return items


class TestStripInterruptionSections(unittest.TestCase):
    """中断区块剔除单元测试"""

    def test_no_interruption_text_unchanged(self):
        text = '姓名：张三\n缴费年度 缴费月份\n2026 6（月）\n'
        self.assertEqual(_strip_interruption_sections(text), text)

    def test_interruption_same_line_removed(self):
        text = MEDICAL_HEADER + '中断起止时间 201805-202104\n'
        stripped = _strip_interruption_sections(text)
        self.assertNotIn('201805', stripped)
        self.assertNotIn('202104', stripped)
        self.assertIn('缴费年度', stripped)

    def test_interruption_data_on_next_lines_removed(self):
        text = MEDICAL_HEADER + '中断信息明细\n中断起止时间\n201805-202104\n'
        stripped = _strip_interruption_sections(text)
        self.assertNotIn('201805', stripped)

    def test_multiple_interruption_rows_removed(self):
        text = MEDICAL_HEADER + '中断信息明细\n201805-202104\n201001-201112\n'
        stripped = _strip_interruption_sections(text)
        self.assertNotIn('201805', stripped)
        self.assertNotIn('201001', stripped)

    def test_content_after_interruption_section_kept(self):
        text = MEDICAL_HEADER + '中断信息明细\n201805-202104\n本证明仅作参考\n'
        stripped = _strip_interruption_sections(text)
        self.assertNotIn('201805', stripped)
        self.assertIn('本证明仅作参考', stripped)


class TestInterruptionNotParsedAsPeriod(unittest.TestCase):
    """规则2：间断参保只计最近连续段（中断信息不得被当参保时间段）"""

    def test_text_path_six_digit_interruption(self):
        text = MEDICAL_HEADER + '中断信息明细\n中断起止时间 201805-202104\n'
        self.assertEqual(get_full_period(text), ('2021-05', '2026-07'))

    def test_text_path_chinese_date_interruption(self):
        text = MEDICAL_HEADER + '中断起止时间：2018年05月至2021年04月\n'
        self.assertEqual(get_full_period(text), ('2021-05', '2026-07'))

    def test_text_path_dot_date_interruption(self):
        text = MEDICAL_HEADER + '中断时间 2018.05-2021.04\n'
        self.assertEqual(get_full_period(text), ('2021-05', '2026-07'))

    def test_text_path_keyword_style_interruption(self):
        text = MEDICAL_HEADER + '中断起始时间 2018-05 截止时间 2021-04\n'
        self.assertEqual(get_full_period(text), ('2021-05', '2026-07'))

    def test_items_path_with_interruption(self):
        """生产路径 parse_ocr_result_from_image → get_full_period_from_items"""
        self.assertEqual(get_full_period_from_items(_medical_items()), ('2021-05', '2026-07'))

    def test_no_interruption_baseline(self):
        """无中断信息时结果不变（回归）"""
        self.assertEqual(get_full_period(MEDICAL_HEADER), ('2021-05', '2026-07'))

    def test_user_example_two_segments(self):
        """用户规则2例子：2021-01~2023-05 + 2024-10~2026-07 → 仅计最近段"""
        periods = extract_periods('参保时间 2021-01至2023-05 又 2024-10至2026-07')
        latest = _keep_latest_segment(periods)
        self.assertEqual(latest, [('2024-10', '2026-07')])


class TestSingleLatestYearRule(unittest.TestCase):
    """规则3：仅最近年度有参保记录 → 起始月向前倒推（2026年6个月→2026-02~2026-07）"""

    SINGLE_YEAR_TEXT = '''城镇职工基本医疗保险参保缴费证明
姓名：张三 身份证号：610502199001011234
缴费年度 缴费月份 缴费年度 缴费月份 缴费年度 缴费月份
2024 0（月） 2025 0（月） 2026 6（月）
'''

    def test_single_year_text_path(self):
        self.assertEqual(get_full_period(self.SINGLE_YEAR_TEXT), ('2026-02', '2026-07'))

    def test_single_year_items_path(self):
        items = []
        xs = [100, 200, 400, 500, 700, 800]
        for i, x in enumerate(xs):
            items.append(_mk_item('缴费年度' if i % 2 == 0 else '缴费月份', x, 100))
        for text, x in [('2024', 100), ('0（月）', 200), ('2025', 400), ('0（月）', 500),
                        ('2026', 700), ('6（月）', 800)]:
            items.append(_mk_item(text, x, 150))
        self.assertEqual(get_full_period_from_items(items), ('2026-02', '2026-07'))

    def test_single_year_with_interruption_info(self):
        """组合场景：仅最近年度有参保 + 存在中断信息 → 仍为 2026-02~2026-07"""
        text = self.SINGLE_YEAR_TEXT + '中断信息明细\n中断起止时间 202408-202512\n'
        self.assertEqual(get_full_period(text), ('2026-02', '2026-07'))


class TestPeriodValidationRobustness(unittest.TestCase):
    """模式1（6位数字对）范围校验：垃圾数字对不得产出时间段"""

    def test_garbage_six_digit_pair_rejected(self):
        # "500000-600000" 解析为 ('5000-00','6000-00') 属垃圾段，应被 make_period 拦截
        periods = extract_periods('缴费基数区间 500000-600000 说明')
        self.assertEqual(periods, [])

    def test_valid_six_digit_pair_kept(self):
        periods = extract_periods('202401-202412')
        self.assertEqual(periods, [('2024-01', '2024-12')])


class TestGroupByPersonLatestSegment(unittest.TestCase):
    """分组汇总层：同人同险种多张证明存在间断时只计最近连续段"""

    def test_multi_records_keep_latest(self):
        records = [
            {'name': '刘伟民', 'idcard': '610502197505123456', 'insurance_type': '医疗保险',
             'period': ('2016-11', '2018-04')},
            {'name': '刘伟民', 'idcard': '610502197505123456', 'insurance_type': '医疗保险',
             'period': ('2021-05', '2026-07')},
        ]
        persons = group_by_person(records)
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0]['insurances']['医疗保险'], ('2021-05', '2026-07'))


class TestFrontendInteraction(unittest.TestCase):
    """规则1：前端交互优化的关键代码存在性验证（静态检查）"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'modules/insurance/static/js/app.js'), encoding='utf-8') as f:
            cls.app_js = f.read()
        with open(os.path.join(root, 'modules/insurance/static/css/style.css'), encoding='utf-8') as f:
            cls.css = f.read()

    def test_render_result_preserves_panel_state(self):
        # 重渲染不得强制收起：className 拼接必须基于 keepExpanded 判断
        self.assertIn('keepExpanded', self.app_js)
        self.assertIn("collapsible-panel' + (keepExpanded ? '' : ' collapsed')", self.app_js)
        # 旧的强制收起写法不得再出现
        self.assertNotIn("orgBox.className = 'organize-info collapsible-panel collapsed';", self.app_js)

    def test_scroll_to_image_row_helper(self):
        self.assertIn('function scrollToImageRow(filename)', self.app_js)
        self.assertIn("scrollIntoView({ block: 'center'", self.app_js)

    def test_save_flows_pass_scroll_target(self):
        self.assertIn('renderResult(data, { scrollToFilename: savedFilename });', self.app_js)
        self.assertIn('var savedFilename = currentEhFilename;', self.app_js)
        self.assertIn('var savedFilename = currentManualFillFilename;', self.app_js)

    def test_highlight_css_exists(self):
        self.assertIn('tr.row-just-saved td', self.css)


if __name__ == '__main__':
    unittest.main()
