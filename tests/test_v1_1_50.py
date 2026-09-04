# -*- coding: utf-8 -*-
"""v1.1.50 缴费单位误解析为"序号"修复测试

Bug A（用户报告）：失业保险记录单的缴费单位老是解析成"序号"。
根因：extract_company_name 的"下一行兜底"无校验——OCR 漏识别
"现缴费单位名称"后面的公司值时，下一行恰是表头"序号 缴费年度 …"，
正则直接取前 2 个中文返回"序号"（标签行全丢时则返回"经办机构"）。

修复：
1. 候选必须通过 _is_valid_company 校验（长度≥3 且不含表头/标签碎片）
2. 新增全文投票兜底：按公司后缀（公司/集团/厂/矿…）提取候选，
   强后缀池优先、频次高者胜（表格各行重复同一单位名，天然多数票）
3. 换行截断拼接：单元格内"…有限公"+"司"跨数据行也能补全
4. 冒号丢失粘连：_clean_company_candidate 切除前粘标签

Bug B（复现时顺带发现）：姓名"薛宇行"被贪婪匹配成"薛宇行个"
（去空格后"薛宇行个人编号"4字截取）。修复：主策略改惰性匹配，
遇下一字段标签开头（_NAME_FOLLOWERS）即截停。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance.core.data_parser import (
    extract_company_name, extract_name, _is_valid_company,
    _clean_company_candidate, _extend_wrapped_company,
)

# 截图（陕西省社会保险权益记录单·失业保险）的真实 OCR 文本
REAL_TEXT = '''陕西省社会保险权益记录单（失业保险）
姓名：薛宇行 个人编号：612052142345
证件号码：610524200006026016
现缴费单位名称：陕西陕煤澄合矿业有限公司 单位：元（小数点后保留两位）
序号 缴费年度 实缴月份 实缴月数 单位缴费 个人缴费 对应缴费单位名称 经办机构
陕西陕煤澄合矿业有限公
1 2023 202304-202312 9 272.94 116.97 澄城县
司
陕西陕煤澄合矿业有限公
2 2024 202401-202412 12 597.12 255.96 澄城县
司
现参保经办机构：澄城县 打印时间：20260819'''

COMPANY = '陕西陕煤澄合矿业有限公司'
TOP_LINE = '现缴费单位名称：陕西陕煤澄合矿业有限公司 单位：元（小数点后保留两位）'


class TestCompanyNormal(unittest.TestCase):
    """1. 正常版式不回归"""

    def test_real_text_company_and_name(self):
        self.assertEqual(extract_company_name(REAL_TEXT), COMPANY)
        self.assertEqual(extract_name(REAL_TEXT), '薛宇行')

    def test_simple_next_line_still_works(self):
        text = '现缴费单位名称：\n鲁岳企业服务（陕西）有限公司\n其他内容'
        # v1.1.34 设计：括号及括号内内容会被去掉，只保留括号外公司名
        self.assertEqual(extract_company_name(text), '鲁岳企业服务有限公司')

    def test_parentheses_stripped(self):
        text = '现缴费单位名称：鲁岳企业服务有限公司（西安分公司）'
        self.assertEqual(extract_company_name(text), '鲁岳企业服务有限公司')


class TestCompanyMisparse(unittest.TestCase):
    """2. Bug A 核心场景：公司值漏识别时绝不能再返回'序号/经办机构'"""

    def test_missing_value_not_xuhao(self):
        bad = REAL_TEXT.replace(TOP_LINE, '现缴费单位名称：')
        r = extract_company_name(bad)
        self.assertNotIn(r, ('序号', '经办机构', '单位缴费', '个人缴费'))
        self.assertEqual(r, COMPANY)  # 投票+跨行碎片补全完整名

    def test_missing_whole_line_not_jingban(self):
        bad = REAL_TEXT.replace(TOP_LINE + '\n', '')
        self.assertEqual(extract_company_name(bad), COMPANY)

    def test_glued_label_without_colon(self):
        bad = REAL_TEXT.replace('现缴费单位名称：陕西', '现缴费单位名称陕西')
        self.assertEqual(extract_company_name(bad), COMPANY)

    def test_truncated_top_line_completed_by_votes(self):
        bad = REAL_TEXT.replace(TOP_LINE, '现缴费单位名称：陕西陕煤澄合矿业有限公 单位：元（小数点后保留两位）')
        self.assertEqual(extract_company_name(bad), COMPANY)

    def test_only_table_rows_voting(self):
        # 无任何关键词行：纯表格重复公司名也能投票得出
        text = '序号 缴费年度 对应缴费单位名称 经办机构\n鲁岳企业服务有限公\n1 2024 澄城县\n司\n鲁岳企业服务有限公\n2 2025 澄城县\n司'
        self.assertEqual(extract_company_name(text), '鲁岳企业服务有限公 司'.replace(' ', ''))

    def test_empty_text(self):
        self.assertEqual(extract_company_name(''), '')
        self.assertEqual(extract_company_name('完全没有单位的文本'), '')


class TestCompanyHelpers(unittest.TestCase):
    """3. 辅助函数"""

    def test_is_valid_company_rejects_labels(self):
        for junk in ('序号', '经办机构', '单位缴费', '个人缴费', '缴费年度', '实缴月数', '', '元', 'AB'):
            self.assertFalse(_is_valid_company(junk), junk)

    def test_is_valid_company_accepts(self):
        for ok in ('陕西陕煤澄合矿业有限公司', '鲁岳企业服务有限公司', '大明厂'):
            self.assertTrue(_is_valid_company(ok), ok)

    def test_clean_glued_label(self):
        self.assertEqual(_clean_company_candidate('现缴费单位名称陕西陕煤澄合矿业有限公司'), COMPANY)
        self.assertEqual(_clean_company_candidate('对应缴费单位名称鲁岳企业服务有限公司'), '鲁岳企业服务有限公司')

    def test_extend_wrapped_company(self):
        lines = ['陕西陕煤澄合矿业有限公', '1 2023 数据行', '司']
        self.assertEqual(_extend_wrapped_company(lines[0], lines, 1), COMPANY)
        # 已完整的不再拼接
        self.assertEqual(_extend_wrapped_company(COMPANY, lines, 1), COMPANY)


class TestNameLazyStop(unittest.TestCase):
    """4. Bug B：姓名惰性截停（防'薛宇行个人编号'→'薛宇行个'）"""

    def test_name_before_gerenbianhao(self):
        self.assertEqual(extract_name('姓名：薛宇行 个人编号：612052142345'), '薛宇行')

    def test_name_before_idcard_label(self):
        self.assertEqual(extract_name('姓名：张三 身份证号：610100199001011234'), '张三')

    def test_four_char_name_before_label(self):
        self.assertEqual(extract_name('姓名：欧阳大强 性别：男'), '欧阳大强')

    def test_two_char_name_end_of_line(self):
        self.assertEqual(extract_name('姓名：李四'), '李四')

    def test_name_merged_with_idcard(self):
        self.assertEqual(extract_name('姓名：张三丰身份证号610100199001011234'), '张三丰')

    def test_label_still_rejected(self):
        # 姓名值缺失时不能返回标签
        r = extract_name('姓名：\n身份证号：610100199001011234')
        self.assertNotIn(r, ('身份证号', '身份证', '号码'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
