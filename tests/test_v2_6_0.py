# -*- coding: utf-8 -*-
"""v2.6.0 江苏省份板块 + 一单多险 + 明细行时间段口径 测试

覆盖：
1. 省份注册：陕西/江苏/浙江三省内置，陕西默认置顶
2. 一单多险展开：江苏/浙江模板把一张单展开为 养老/工伤/失业 三条记录
3. 单险省份零变化：陕西与兼容模式恒单条，时间段口径不变
4. 明细行时间段：_parse_year_month_columns 起点校正（绕开首行 0/1 混淆）
5. 月份连续性校正：_fix_month_continuity 边界
6. 上传链路：_ocr_one_image 返回 list（一单多险展平）
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance.core import template_engine as te
from modules.insurance.core import data_parser as dp


def _item(text, x, y, score=0.99):
    return {'text': text, 'x': x, 'y': y, 'score': score}


class TestProvinceRegistration(unittest.TestCase):
    """三省内置 + 默认省份置顶"""

    def test_three_provinces_builtin(self):
        codes = [p['province_code'] for p in te.get_provinces()]
        self.assertEqual(codes, ['610000', '320000', '330000'])

    def test_default_province_is_shaanxi(self):
        first = te.get_provinces()[0]
        self.assertEqual(first['province_code'], '610000')
        self.assertTrue(first['default'])
        self.assertEqual(te.DEFAULT_PROVINCE, '610000')

    def test_shaanxi_has_no_multi_insurance(self):
        """陕西保持单险种，multi_insurance 为空"""
        tpl, _s, _e = te.match_template('参保 缴费 社会保险', '610000')
        self.assertEqual(tpl.multi_insurance, [])
        self.assertFalse(tpl.actual_payment_end)

    def test_jiangsu_template_meta(self):
        tpl, _s, _e = te.match_template(
            '江苏智慧人社 江苏省社会保险权益记录单 现参保单位全称', '320000')
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl.template_id, '320000_si_2026')
        self.assertEqual(tpl.province_name, '江苏')
        self.assertEqual(tpl.multi_insurance,
                         ['养老保险', '工伤保险', '失业保险'])
        self.assertTrue(tpl.actual_payment_end)
        self.assertEqual(tpl.period.get('table_parser'), 'year_month_columns')

    def test_zhejiang_template_meta(self):
        tpl, _s, _e = te.match_template(
            '浙江省社会保险参保证明 单位编号 缴费状况', '330000')
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl.template_id, '330000_si_2026')
        self.assertEqual(tpl.province_name, '浙江')
        self.assertEqual(tpl.multi_insurance,
                         ['养老保险', '工伤保险', '失业保险'])
        self.assertTrue(tpl.actual_payment_end)


class TestMultiInsuranceExpansion(unittest.TestCase):
    """一单多险展开"""

    def test_expand_creates_one_record_per_insurance(self):
        base = {'insurance_type': '失业保险', 'name': '张三', 'idcard': '610101199001011234',
                'company_name': '某公司', 'period': ('2023-09', '2026-08'), 'raw_text': ''}
        recs = te._expand_multi_insurance(base, ['养老保险', '工伤保险', '失业保险'])
        self.assertEqual(len(recs), 3)
        self.assertEqual([r['insurance_type'] for r in recs],
                         ['养老保险', '工伤保险', '失业保险'])
        # 其余字段保持一致
        for r in recs:
            self.assertEqual(r['name'], '张三')
            self.assertEqual(r['period'], ('2023-09', '2026-08'))

    def test_expand_does_not_mutate_base(self):
        base = {'insurance_type': '失业保险', 'name': '李四', 'period': None}
        te._expand_multi_insurance(base, ['养老保险'])
        self.assertEqual(base['insurance_type'], '失业保险')

    def test_parse_multi_single_insurance_template(self):
        """单险模板 parse_multi 恒返回单元素列表"""
        tpl, _s, _e = te.match_template('参保 缴费', '610000')
        with mock.patch.object(te, '_builtin_shaanxi_parse',
                               return_value={'insurance_type': '养老保险', 'name': '王五'}):
            out = tpl.parse_multi('dummy text')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['insurance_type'], '养老保险')

    def test_parse_multi_multi_insurance_template(self):
        """多险模板 parse_multi 展开为三条"""
        tpl, _s, _e = te.match_template(
            '江苏智慧人社 江苏省社会保险权益记录单', '320000')
        with mock.patch.object(te, '_builtin_shaanxi_parse',
                               return_value={'insurance_type': '失业保险', 'name': '赵六'}):
            out = tpl.parse_multi('dummy text')
        self.assertEqual(len(out), 3)
        self.assertEqual([r['insurance_type'] for r in out],
                         ['养老保险', '工伤保险', '失业保险'])


class TestMonthContinuityFix(unittest.TestCase):
    """月份连续性校正（0/1 混淆）"""

    def test_normal_sequence_untouched(self):
        seq = [(2024, 1), (2024, 2), (2024, 3)]
        self.assertEqual(te._fix_month_continuity(seq), seq)

    def test_cross_year_ok(self):
        seq = [(2025, 12), (2026, 1), (2026, 2)]
        self.assertEqual(te._fix_month_continuity(seq), seq)

    def test_reverse_fixed(self):
        """逆序（如 2024-10 实际应为 2024-01）按连续回推"""
        out = te._fix_month_continuity([(2023, 12), (2024, 10)])
        self.assertEqual(out, [(2023, 12), (2024, 1)])

    def test_single_item(self):
        self.assertEqual(te._fix_month_continuity([(2024, 5)]), [(2024, 5)])

    def test_empty(self):
        self.assertEqual(te._fix_month_continuity([]), [])


class TestYearMonthColumnsParser(unittest.TestCase):
    """江苏/浙江明细行时间段解析"""

    def _sample_items(self, n_rows=36, start=(2023, 9), declared=37):
        """构造江苏版样例：年列 x≈100、月列 x≈155、公司名 x≈180、
        声明行含"前37个月"、月份列首行故意误读（09→10）"""
        items = [
            _item('年', 100, 636),
            _item('月', 155, 637),
            _item('单位全称', 238, 636),
            _item('出具证明前%d个月缴费情况（202309-202609）' % declared, 400, 660),
        ]
        y = 700
        sy, sm = start
        total = sy * 12 + sm
        for i in range(n_rows):
            idx = total + i
            yy, mm = divmod(idx, 12)
            if mm == 0:
                yy, mm = yy - 1, 12
            # 首行故意误读为 10（模拟 0/1 混淆）
            mm_text = '10' if i == 0 else '%02d' % mm
            items.append(_item('%d' % yy, 100, y))
            items.append(_item(mm_text, 155, y + 2))
            items.append(_item('苏州博达特机电科技有限公司', 180, y - 5))
            y += 40
        return items

    def test_start_corrected_by_row_count(self):
        """起点按明细行数反推，绕开首行误读"""
        items = self._sample_items(n_rows=36, start=(2023, 9))
        got = te._parse_year_month_columns(items, '')
        self.assertIsNotNone(got)
        start, end = got
        self.assertEqual(start, '2023-09')
        self.assertEqual(end, '2026-08')

    def test_row_count_excludes_header_area_company(self):
        """行数统计排除表头之上的"现参保单位全称"值行"""
        items = self._sample_items(n_rows=36)
        items.append(_item('苏州博达特机电科技有限公司', 369, 540))  # 表头之上
        n = te._count_detail_rows(items, 100)
        self.assertEqual(n, 36)

    def test_fallback_when_no_company_rows(self):
        """无公司名块时退化为年份列计数"""
        items = [_item('年', 100, 636), _item('月', 155, 637)]
        y = 700
        for i in range(5):
            items.append(_item('%d' % (2024 + (i // 12)), 100, y))
            items.append(_item('%02d' % ((i % 12) + 1), 155, y + 2))
            y += 40
        self.assertEqual(te._count_detail_rows(items, 100), 5)

    def test_no_header_returns_none(self):
        self.assertIsNone(te._parse_year_month_columns([_item('正文', 10, 10)], ''))

    def test_empty_items_returns_none(self):
        self.assertIsNone(te._parse_year_month_columns([], ''))


class TestPickPeriodProvinceDifference(unittest.TestCase):
    """时间段口径：江苏走明细行，陕西走原逻辑"""

    def test_shaanxi_uses_original_logic(self):
        tpl, _s, _e = te.match_template('参保 缴费', '610000')
        items = [_item('年', 100, 636), _item('月', 155, 637)]
        with mock.patch.object(dp, 'get_full_period_from_items',
                               return_value=('2020-01', '2021-01')) as m:
            got = te._pick_period(dp, 'text', items, tpl)
        self.assertEqual(got, ('2020-01', '2021-01'))
        m.assert_called_once()

    def test_jiangsu_prefers_table_parser(self):
        tpl, _s, _e = te.match_template(
            '江苏智慧人社 江苏省社会保险权益记录单', '320000')
        items = [_item('年', 100, 636), _item('月', 155, 637)]
        with mock.patch.object(te, '_parse_year_month_columns',
                               return_value=('2023-09', '2026-08')) as m:
            got = te._pick_period(dp, 'text', items, tpl)
        self.assertEqual(got, ('2023-09', '2026-08'))
        m.assert_called_once()

    def test_jiangsu_fallback_when_parser_empty(self):
        """明细解析取不到时回退原逻辑，不至于丢值"""
        tpl, _s, _e = te.match_template(
            '江苏智慧人社 江苏省社会保险权益记录单', '320000')
        with mock.patch.object(te, '_parse_year_month_columns', return_value=None), \
             mock.patch.object(dp, 'get_full_period_from_items',
                               return_value=('2020-01', '2021-01')):
            got = te._pick_period(dp, 'text', [], tpl)
        self.assertEqual(got, ('2020-01', '2021-01'))


class TestOcrOneImageReturnsList(unittest.TestCase):
    """上传链路：_ocr_one_image 返回 list（一单多险展平）"""

    def test_returns_list_of_records(self):
        from modules.insurance import blueprint as ins_bp
        fake = [{'insurance_type': '养老保险', 'name': '张三'},
                {'insurance_type': '失业保险', 'name': '张三'}]
        with mock.patch.object(ins_bp, 'parse_ocr_result_from_image_multi',
                               return_value=fake):
            out = ins_bp._ocr_one_image('f.jpg', '320000', 'a.jpg', 'a.jpg')
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]['filename'], 'a.jpg')
        self.assertEqual(out[1]['insurance_type'], '失业保险')

    def test_exception_returns_single_error_list(self):
        from modules.insurance import blueprint as ins_bp
        with mock.patch.object(ins_bp, 'parse_ocr_result_from_image_multi',
                               side_effect=RuntimeError('boom')):
            out = ins_bp._ocr_one_image('f.jpg', '320000', 'a.jpg', 'a.jpg')
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        self.assertIn('error', out[0])


if __name__ == '__main__':
    unittest.main()
