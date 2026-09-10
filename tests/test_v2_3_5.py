# -*- coding: utf-8 -*-
"""v2.3.5 MCP 修复与增强测试

需求覆盖：
1. **修复** insurance_calculate 的 year_range 格式化缺陷：
   _year_range 返回 ('YYYY-MM','YYYY-MM') 字符串对，原实现却用
   '%04d-%02d ~ %04d-%02d' 做整数格式化 → TypeError
   ("%d format: a real number is required, not str")。
   该语句位于 wait_seconds 分支之前，导致：
     - 凡带统计时间段的调用一律 isError（任务却已启动，调用方误判失败）
     - wait_seconds 服务端等待功能整体失效
2. **新增** 社保核算两阶段预览确认（preview_only / confirm_task_id）：
   预览只做文件清点 + 花名册概览 + 姓名预比对，不执行 OCR；
   确认时可用同一次调用覆盖参数。
3. 清点与预比对工具函数（险种判定、文件名拆解、名单比对）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mcp.core import adapters, tasks
from modules.mcp.core import tools as tool_registry


# ---------------------------------------------------------------- 工具函数单测

class TestProofInventory(unittest.TestCase):
    """参保证明清点：险种判定 / 文件名拆解 / 统计"""

    def test_guess_insurance_type_from_dir(self):
        cases = [
            ('D:/样本/参保证明1/4.养老参保证明/103 王冬.png', '养老保险'),
            ('D:/样本/参保证明1/6.失业参保证明/103 王冬.jpg', '失业保险'),
            ('D:/样本/参保证明1/7.医疗参保证明/103王冬.png', '医疗保险'),
            ('D:/样本/参保证明1/9.工伤参保证明/103 王冬.png', '工伤保险'),
            ('D:/随便/103 王冬.png', '未识别'),
        ]
        for path, expect in cases:
            self.assertEqual(adapters.guess_insurance_type(path), expect, path)

    def test_inventory_groups_by_type_and_extension(self):
        """清点只报份数与分布，不含任何命名解析/异常命名判定"""
        paths = [
            'X/4.养老参保证明/103 王冬.png',
            'X/4.养老参保证明/104 刘栋.png',
            'X/6.失业参保证明/103 王冬.jpg',
            'X/7.医疗参保证明/103王冬.png',
            'X/7.医疗参保证明/无法识别.png',
        ]
        inv = adapters.inventory_insurance_files(paths)
        self.assertEqual(inv['total'], 5)
        self.assertEqual(inv['by_insurance_type']['养老保险'], 2)
        self.assertEqual(inv['by_insurance_type']['失业保险'], 1)
        self.assertEqual(inv['by_insurance_type']['医疗保险'], 2)
        self.assertEqual(inv['by_extension']['.png'], 4)
        self.assertEqual(inv['by_extension']['.jpg'], 1)

    def test_mcp_layer_has_no_naming_logic(self):
        """角色边界：MCP 层不得承载命名解析/匹配（2026-09-10 用户确立）"""
        for gone in ('split_proof_filename', 'match_proofs_to_roster'):
            self.assertFalse(hasattr(adapters, gone), gone)
        inv = adapters.inventory_insurance_files(['X/4.养老参保证明/103 王冬.png'])
        for field in ('naming_shapes', 'abnormal_name_count', 'abnormal_names'):
            self.assertNotIn(field, inv, field)


# ---------------------------------------------------------------- year_range 修复

class TestYearRangeFix(unittest.TestCase):
    """v2.3.5 缺陷修复：year_range 只能按 %s 拼接"""

    def test_year_range_returns_string_pair(self):
        rng = tool_registry._year_range({'year_start': 2023, 'month_start': 1,
                                         'year_end': 2025, 'month_end': 12})
        self.assertEqual(rng, ('2023-01', '2025-12'))

    def test_string_pair_cannot_use_int_format(self):
        """锁定缺陷本身：字符串对做 %04d 格式化必然抛 TypeError"""
        with self.assertRaises(TypeError) as cm:
            '%04d-%02d ~ %04d-%02d' % ('2023-01', '2025-12')
        self.assertIn('a real number is required, not str', str(cm.exception))

    def test_fixed_expression_formats(self):
        rng = ('2023-01', '2025-12')
        self.assertEqual('%s ~ %s' % (rng[0], rng[1]), '2023-01 ~ 2025-12')


# ---------------------------------------------------------------- 预览 / 确认

class _Base(unittest.TestCase):
    """构造临时参保证明目录 + 花名册"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proofs_dir = os.path.join(self.tmp, '参保证明1')
        layout = [
            ('4.养老参保证明', '.png', ['103 王冬', '104 刘栋']),
            ('6.失业参保证明', '.jpg', ['103 王冬', '104 刘栋']),
            ('7.医疗参保证明', '.png', ['103王冬', '104刘栋']),
        ]
        for sub, ext, names in layout:
            d = os.path.join(self.proofs_dir, sub)
            os.makedirs(d)
            for n in names:
                with open(os.path.join(d, n + ext), 'wb') as f:
                    f.write(b'\x89PNG\r\n\x1a\n')
        self.roster = os.path.join(self.tmp, '花名册.xlsx')
        self._write_roster(self.roster, [
            ('王冬', '610527199006135611', '脱贫人口'),
            ('刘栋', '610525198711190432', '脱贫人口'),
            ('张三', '610525199001011234', '脱贫人口'),   # 无证明文件
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write_roster(path, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(['序号', '姓名', '身份证号', '身份类型', '劳动合同起止时间'])
        for i, (name, idc, itype) in enumerate(rows, 1):
            ws.append([i, name, idc, itype, '202001-无固定期限'])
        wb.save(path)

    def _preview_args(self, **over):
        args = {
            'file_paths': [self.proofs_dir],
            'province': '610000',
            'roster_path': self.roster,
            'year_start': 2023, 'month_start': 1,
            'year_end': 2025, 'month_end': 12,
            'preview_only': True,
        }
        args.update(over)
        return args


class TestInsurancePreview(_Base):

    def test_preview_does_not_start_ocr(self):
        with mock.patch.object(adapters, 'start_insurance') as m:
            text, is_error = tool_registry.tool_insurance_calculate(self._preview_args())
            m.assert_not_called()
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'waiting_confirm')
        self.assertEqual(payload['effective_params']['province'], '610000')
        self.assertEqual(payload['effective_params']['tax_mode'], '退税')
        self.assertEqual(payload['effective_params']['year_range'], '2023-01 ~ 2025-12')
        self.assertEqual(payload['effective_params']['file_count'], 6)

    def test_preview_inventory_and_roster(self):
        """预览只报份数分布与花名册概览，不含命名解析与姓名匹配"""
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        payload = json.loads(text)
        inv = payload['inventory']
        self.assertEqual(inv['total'], 6)
        self.assertEqual(inv['by_insurance_type']['养老保险'], 2)
        self.assertEqual(inv['by_insurance_type']['医疗保险'], 2)
        self.assertEqual(payload['roster']['person_count'], 3)
        self.assertEqual(payload['roster']['identity_type_dist'], {'脱贫人口': 3})
        # 命名/匹配相关字段一律不再出现在预览响应里
        self.assertNotIn('prematch', payload)
        for field in ('naming_shapes', 'abnormal_name_count', 'abnormal_names'):
            self.assertNotIn(field, inv, field)

    def test_preview_task_state_is_waiting_confirm(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        payload = json.loads(text)
        task = tasks.get(payload['task_id'])
        self.assertEqual(task['status'], 'waiting_confirm')
        self.assertIsNotNone(task.get('pending_insurance'))
        # 预览阶段不建原生任务
        self.assertIsNone(task.get('native_task_id'))

    def test_preview_without_roster_still_works(self):
        args = self._preview_args()
        args.pop('roster_path')
        text, is_error = tool_registry.tool_insurance_calculate(args)
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['roster']['person_count'], 0)
        self.assertNotIn('prematch', payload)

    def test_preview_status_hint_for_insurance(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        tid = json.loads(text)['task_id']
        view_text, _ = tool_registry.tool_get_task_status({'task_id': tid})
        view = json.loads(view_text)
        self.assertEqual(view['status'], 'waiting_confirm')
        self.assertIn('insurance_calculate', view['hint'])
        self.assertNotIn('contract_organize', view['hint'])


class TestInsuranceConfirm(_Base):

    def test_confirm_starts_execution(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_insurance') as m:
            out, is_error = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': tid})
            m.assert_called_once()
        self.assertFalse(is_error)
        self.assertEqual(json.loads(out)['task_id'], tid)
        _args, kwargs = m.call_args
        self.assertEqual(kwargs['tax_mode'], '退税')
        self.assertEqual(kwargs['roster_path'], self.roster)
        self.assertEqual(kwargs['year_range'], ('2023-01', '2025-12'))

    def test_confirm_can_override_params(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_insurance') as m:
            tool_registry.tool_insurance_calculate({
                'confirm_task_id': tid,
                'tax_mode': '抵税',
                'year_start': 2024, 'month_start': 1,
                'year_end': 2024, 'month_end': 12,
            })
        _args, kwargs = m.call_args
        self.assertEqual(kwargs['tax_mode'], '抵税')
        self.assertEqual(kwargs['year_range'], ('2024-01', '2024-12'))

    def test_confirm_partial_year_range_rejected(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_insurance') as m:
            out, is_error = tool_registry.tool_insurance_calculate({
                'confirm_task_id': tid, 'year_start': 2024})
            m.assert_not_called()
        self.assertTrue(is_error)
        self.assertIn('四项同时提供',
                      json.dumps(json.loads(out), ensure_ascii=False))

    def test_confirm_unknown_task(self):
        out, is_error = tool_registry.tool_insurance_calculate(
            {'confirm_task_id': 'nope1234'})
        self.assertTrue(is_error)
        self.assertIn('不存在', json.loads(out)['error'])

    def test_confirm_rejected_when_not_waiting(self):
        tid = tasks.create('insurance', {'file_count': 1})
        tasks.update(tid, status='processing')
        out, is_error = tool_registry.tool_insurance_calculate(
            {'confirm_task_id': tid})
        self.assertTrue(is_error)
        self.assertIn('waiting_confirm', json.loads(out)['error'])

    def test_confirm_twice_rejected(self):
        text, _ = tool_registry.tool_insurance_calculate(self._preview_args())
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_insurance'):
            tool_registry.tool_insurance_calculate({'confirm_task_id': tid})
            out, is_error = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': tid})
        self.assertTrue(is_error)


# ---------------------------------------------------------------- 缺陷回归

class TestWaitPathRegression(_Base):
    """原缺陷使 wait_seconds 永远走不到：effective 构造抛错在其之前"""

    def test_wait_seconds_reached_with_year_range(self):
        with mock.patch.object(adapters, 'start_insurance'):
            with mock.patch.object(tool_registry, '_wait_and_fetch',
                                   return_value=('{}', False)) as m:
                args = self._preview_args()
                args.pop('preview_only')
                args['wait_seconds'] = 600
                tool_registry.tool_insurance_calculate(args)
                m.assert_called_once()

    def test_immediate_payload_carries_effective_params(self):
        with mock.patch.object(adapters, 'start_insurance'):
            args = self._preview_args()
            args.pop('preview_only')
            text, is_error = tool_registry.tool_insurance_calculate(args)
        self.assertFalse(is_error)
        eff = json.loads(text)['effective_params']
        self.assertEqual(eff['year_range'], '2023-01 ~ 2025-12')

    def test_no_year_range_uses_placeholder(self):
        with mock.patch.object(adapters, 'start_insurance'):
            text, is_error = tool_registry.tool_insurance_calculate({
                'file_paths': [self.proofs_dir], 'province': '610000'})
        self.assertFalse(is_error)
        eff = json.loads(text)['effective_params']
        self.assertEqual(eff['year_range'], '参保数据全区间')


# ---------------------------------------------------------------- schema

class TestToolSchema(unittest.TestCase):

    def test_preview_and_confirm_declared(self):
        props = tool_registry.TOOLS['insurance_calculate']['inputSchema']['properties']
        self.assertIn('preview_only', props)
        self.assertIn('confirm_task_id', props)
        self.assertEqual(
            tool_registry.TOOLS['insurance_calculate']['inputSchema']['required'], [])

    def test_description_mentions_two_phase(self):
        desc = tool_registry.TOOLS['insurance_calculate']['description']
        self.assertIn('preview_only', desc)
        self.assertIn('confirm_task_id', desc)


if __name__ == '__main__':
    unittest.main()
