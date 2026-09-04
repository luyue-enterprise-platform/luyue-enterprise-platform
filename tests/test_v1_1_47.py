# -*- coding: utf-8 -*-
"""v1.1.47 社保模块姓名误识别修复测试

Bug：OCR漏识别姓名值时，extract_name 把"姓名："后面紧跟的下一个字段标签
    "身份证号"当成了姓名（结果表姓名列出现大量"身份证号"字样）。

修复：
1. extract_name 拒绝字段标签词（身份证号/编号/单位...）作为姓名
2. 兜底1：OCR合并行——姓名值紧贴"身份证"标签前（"姓名：张三身份证号610..."）
3. 兜底2：下一行前导姓名（须后随空白/行尾，防止误取"现缴费单位名称"长标签）
4. blueprint._split_ocr_results：姓名缺失但身份证号有效 →
   先按身份证号从花名册回填姓名；仍无姓名归入失败桶（原静默丢弃）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance.core.data_parser import extract_name, _is_label_word
from modules.insurance import blueprint as bp


class TestExtractNameLabelRejection(unittest.TestCase):
    """1. 主修复：字段标签词绝不能当姓名"""

    def test_missing_name_not_label(self):
        # 核心场景：OCR漏识别姓名，"姓名："后直接是"身份证号"标签
        text = '姓名： 身份证号：610524198702161211 个人编号：61000060'
        self.assertEqual(extract_name(text), '')

    def test_missing_name_full_label_line(self):
        # 医保证明完整布局，姓名值缺失
        text = ('城镇职工基本医疗保险参保缴费证明\n'
                '姓名： 身份证号：610524198702161211\n'
                '现缴费单位名称： 陕西陕煤澄合矿业有限公司')
        self.assertEqual(extract_name(text), '')

    def test_label_words_rejected(self):
        for w in ('身份证号', '身份证', '个人编号', '参保状态', '缴费年度', '单位'):
            self.assertTrue(_is_label_word(w), w)

    def test_real_name_still_works(self):
        self.assertEqual(extract_name('姓名： 翟海涛  身份证号：61252319871225373X'), '翟海涛')
        self.assertEqual(extract_name('姓名：王五'), '王五')
        self.assertEqual(extract_name('姓 名：张三'), '张三')


class TestExtractNameFallbacks(unittest.TestCase):
    """2/3. 两种兜底恢复"""

    def test_merged_line_name_before_idcard(self):
        # OCR合并行：姓名值紧贴"身份证"标签
        self.assertEqual(extract_name('姓名：张三身份证号610524198702161211'), '张三')

    def test_next_line_standalone_name(self):
        self.assertEqual(extract_name('姓名：\n李四'), '李四')
        self.assertEqual(extract_name('姓名：\n李四 610524198702161211'), '李四')

    def test_next_line_long_label_not_name(self):
        # 下一行是长标签"现缴费单位名称：..."，不能取前4个字"现缴费单位"
        text = '姓名：\n现缴费单位名称： 陕西陕煤澄合矿业有限公司'
        self.assertEqual(extract_name(text), '')

    def test_next_line_label_word_not_name(self):
        text = '姓名：\n身份证号：610524198702161211'
        self.assertEqual(extract_name(text), '')


class TestSplitOcrResults(unittest.TestCase):
    """4. _split_ocr_results：花名册回填 + 失败桶兜底"""

    ID_OK = '11010519491231002X'  # 校验码合法

    def _rec(self, **kw):
        base = {'filename': 'a.jpg', 'error': None, 'name': '', 'idcard': '',
                'insurance_type': '养老保险', 'period': ('2024-01', '2024-12'),
                '_source_path': '/tmp/a.jpg'}
        base.update(kw)
        return base

    def test_fill_name_from_roster(self):
        roster = [{'seq': 1, 'name': '张三', 'idcard': self.ID_OK}]
        recs = [self._rec(idcard=self.ID_OK)]
        ok, failed, files = bp._split_ocr_results('t1', recs, roster)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]['name'], '张三')
        self.assertEqual(len(failed), 0)
        self.assertEqual(files, ['a.jpg'])

    def test_no_name_no_roster_goes_failed(self):
        # 原逻辑：name='' + idcard 有效 → 进成功桶但分组时被静默丢弃
        # 现逻辑：归入失败桶，标"姓名未识别（可手动补录）"
        roster = []
        recs = [self._rec(idcard=self.ID_OK)]
        ok, failed, files = bp._split_ocr_results('t1', recs, roster)
        self.assertEqual(len(ok), 0)
        self.assertEqual(len(failed), 1)
        self.assertIn('姓名未识别', failed[0]['error'])

    def test_error_record_stays_failed(self):
        recs = [self._rec(error='图片损坏')]
        ok, failed, files = bp._split_ocr_results('t1', recs, [])
        self.assertEqual(len(ok), 0)
        self.assertEqual(failed[0]['error'], '图片损坏')

    def test_normal_record_success(self):
        recs = [self._rec(name='李四', idcard='')]
        ok, failed, files = bp._split_ocr_results('t1', recs, [])
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]['name'], '李四')

    def test_neither_name_nor_idcard_failed(self):
        recs = [self._rec()]
        ok, failed, files = bp._split_ocr_results('t1', recs, [])
        self.assertEqual(len(ok), 0)
        self.assertEqual(len(failed), 1)

    def test_roster_none_safe(self):
        # roster 传 None 不报错（process_task 可能拿到空花名册）
        recs = [self._rec(name='王五')]
        ok, failed, files = bp._split_ocr_results('t1', recs, None)
        self.assertEqual(len(ok), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
