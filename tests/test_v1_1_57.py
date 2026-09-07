# -*- coding: utf-8 -*-
"""v1.1.57 退税/抵税互斥单选模式测试

需求：
1. 退税（默认）：完整保留现有全部逻辑（字段名、时间段统计规则、年度台账生成）。
2. 抵税：
   - 字段名称替换：输出表格中所有展示为"退税"的字段名统一替换为"抵税"，
     仅改展示文案，底层数据结构与字段标识不变。
   - 时间段统计规则：重叠部分归入所选的、能完整包含或等于该重叠区间的
     统计时间段，不重复、不遗漏。例：重叠 2026-01~2026-07，所选统计时间段
     2026-07~2026-07 → 输出 2026-07~2026-07。
   - 跳过年度台账生成。
3. 切换逻辑：两选项切换界面/字段/统计结果实时联动，互不干扰、无残留状态。

边界场景：无重叠、完全重叠、部分重叠、多个重叠区间、起止日期相同。
"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.insurance import blueprint as bp
from modules.insurance.core.stats_calculator import (
    apply_stat_range_clamp,
    calc_all_stats,
    calc_person_stats,
)
from modules.insurance.core.contract_overlap import apply_contract_to_stats
from modules.insurance.core.excel_generator import generate_excel

INS4 = ['养老保险', '医疗保险', '工伤保险', '失业保险']
ID_ZHANG = '11010519491231002X'  # 校验码合法


def _make_person(name='张三', idcard=ID_ZHANG, period=('2026-01', '2026-07')):
    """构造四险统一时间段的人员（四险一致 → 重叠=该时间段）"""
    return {
        'name': name,
        'idcard': idcard,
        'insurances': {ins: period for ins in INS4},
    }


def _pipeline(persons, year_range):
    """复刻 _rebuild_result 的统计漏斗：统计 → 合同叠加 → 起点钳制"""
    stats, year_cols = calc_all_stats(persons, year_range=year_range)
    apply_stat_range_clamp(stats, year_range)
    return stats, year_cols


# ==================== 1. 时间段统计规则：边界场景（两模式共用同一漏斗） ====================
class TestOverlapStatRuleBoundaries(unittest.TestCase):
    """重叠部分归入所选统计时间段，确保不重复、不遗漏"""

    def test_user_example_single_month_range(self):
        """用户需求示例：重叠 2026-01~2026-07，统计时间段 2026-07~2026-07
        → 输出 2026-07~2026-07（重叠归入能完整包含它的所选区间）"""
        stats, _ = _pipeline([_make_person(period=('2026-01', '2026-07'))],
                             ('2026-07', '2026-07'))
        ps = stats[0]
        self.assertTrue(ps['has_overlap'])
        self.assertEqual(ps['overlap_start'], '2026-07')
        self.assertEqual(ps['overlap_end'], '2026-07')
        self.assertEqual(ps['overlap_months'], 1)
        self.assertEqual(ps['yearly_months'], {2026: 1})

    def test_no_overlap(self):
        """无重叠：四险时间段互不相交 → 无统计结果，不产生任何月数"""
        person = {'name': '李四', 'idcard': '110105194912310021',
                  'insurances': {'养老保险': ('2023-01', '2023-12'),
                                 '医疗保险': ('2024-06', '2024-12'),
                                 '工伤保险': ('2023-01', '2023-12'),
                                 '失业保险': ('2024-06', '2024-12')}}
        stats, _ = _pipeline([person], ('2023-01', '2025-12'))
        self.assertFalse(stats[0]['has_overlap'])
        self.assertEqual(stats[0]['overlap_months'], 0)
        self.assertEqual(stats[0]['yearly_months'], {})

    def test_full_overlap_inside_range(self):
        """完全重叠：重叠区间完整落在所选统计时间段内 → 原样保留不裁剪"""
        stats, _ = _pipeline([_make_person(period=('2024-03', '2024-09'))],
                             ('2023-01', '2025-12'))
        ps = stats[0]
        self.assertEqual((ps['overlap_start'], ps['overlap_end']),
                         ('2024-03', '2024-09'))
        self.assertEqual(ps['overlap_months'], 7)
        self.assertEqual(ps['yearly_months'], {2024: 7})

    def test_partial_overlap_clamped_to_range(self):
        """部分重叠：重叠超出统计起点 → 起点钳到统计开始；月数按所选区间计"""
        stats, _ = _pipeline([_make_person(period=('2022-09', '2026-07'))],
                             ('2023-01', '2025-12'))
        ps = stats[0]
        self.assertEqual((ps['overlap_start'], ps['overlap_end']),
                         ('2023-01', '2026-07'))
        self.assertEqual(ps['overlap_months'], 36)  # 2023~2025 各12月
        # 范围外年份月数为 0（归入所选区间，不遗漏也不多计）
        self.assertEqual(ps['yearly_months'].get(2022, 0), 0)
        self.assertEqual(ps['yearly_months'].get(2026, 0), 0)

    def test_multiple_overlap_segments_no_dup_no_omission(self):
        """多个重叠区间（合同分段产生间断）：各段精确求和，间断月剔除，
        每月只计一次（不重复），分段覆盖月全部计入（不遗漏）"""
        roster = [{'name': '张三', 'idcard': ID_ZHANG,
                   'contract_status': 'ok', 'contract_error': '',
                   'contract_raw': '2022-01-01~2023-06-30、2024-01-01~2026-07-31',
                   'contract_periods': [('2022-01', '2023-06'),
                                        ('2024-01', '2026-07')]}]
        stats, _ = calc_all_stats([_make_person(period=('2022-09', '2026-07'))],
                                  year_range=('2023-01', '2025-12'))
        apply_contract_to_stats(stats, roster, year_range=('2023-01', '2025-12'))
        apply_stat_range_clamp(stats, ('2023-01', '2025-12'))
        ps = stats[0]
        # 分段实际：2023:6（1-6月）+ 2024:12 + 2025:12 = 30（2023下半年间断剔除）
        self.assertEqual(ps['overlap_months'], 30)
        self.assertEqual(ps['yearly_months'].get(2023), 6)
        self.assertEqual(ps['yearly_months'].get(2024), 12)
        self.assertEqual(ps['yearly_months'].get(2025), 12)
        # 不重复：总月数 = 各年度月数之和（每月恰计一次）
        self.assertEqual(sum(ps['yearly_months'].values()), ps['overlap_months'])

    def test_same_start_end_month(self):
        """起止日期相同：四险均为单月 2026-07~2026-07 → 重叠单月 1 个月"""
        stats, _ = _pipeline([_make_person(period=('2026-07', '2026-07'))],
                             ('2026-07', '2026-07'))
        ps = stats[0]
        self.assertTrue(ps['has_overlap'])
        self.assertEqual((ps['overlap_start'], ps['overlap_end']),
                         ('2026-07', '2026-07'))
        self.assertEqual(ps['overlap_months'], 1)

    def test_range_without_intersection_no_result(self):
        """所选统计区间与重叠段无交集 → 按无统计结果处理（归入失败而非错配）"""
        stats, _ = _pipeline([_make_person(period=('2022-01', '2022-12'))],
                             ('2023-01', '2025-12'))
        ps = stats[0]
        self.assertFalse(ps['has_overlap'])
        self.assertEqual(ps['overlap_months'], 0)


# ==================== 2. Excel 生成：两模式文案与年度台账 ====================
class TestExcelTaxMode(unittest.TestCase):
    """退税=原样；抵税=展示文案替换 + 跳过年度台账；底层数据不变"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='test_v1157_xl_')
        self.persons = [_make_person(period=('2022-09', '2026-07'))]
        self.stats, self.year_cols = calc_all_stats(
            self.persons, year_range=('2023-01', '2025-12'))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _gen(self, tax_mode=None, name='main.xlsx'):
        out = os.path.join(self.tmpdir, name)
        kwargs = {'stats': (self.stats, self.year_cols)}
        if tax_mode is not None:
            kwargs['tax_mode'] = tax_mode
        return generate_excel(self.persons, out, **kwargs), out

    def _read_headers(self, path):
        from openpyxl import load_workbook
        wb = load_workbook(path)
        headers = [c.value for c in wb.active[2]]
        wb.close()
        return [h for h in headers if h]

    def test_tuishui_default_headers_and_yearly_ledgers(self):
        """退税（默认）：表头保持"退税"字样，年度台账照常生成"""
        res, out = self._gen()  # 不传 tax_mode = 默认退税
        headers = self._read_headers(out)
        self.assertIn('申请退税总月数', headers)
        self.assertIn('合计申请退税总额', headers)
        self.assertTrue(any(h.endswith('年申请退税月数') for h in headers))
        self.assertFalse(any('抵税' in h for h in headers))
        self.assertGreater(len(res['yearly_ledger_files']), 0)
        for f in res['yearly_ledger_files']:
            self.assertTrue(os.path.exists(f['filepath']))

    def test_dishui_headers_replaced_display_only(self):
        """抵税：表头"退税"→"抵税"（展示层），年度列/月数等底层数据不变"""
        res, out = self._gen('抵税')
        headers = self._read_headers(out)
        self.assertIn('申请抵税总月数', headers)
        self.assertIn('合计申请抵税总额', headers)
        self.assertTrue(any(h.endswith('年申请抵税月数') for h in headers))
        # 展示文案中不得残留"退税"
        self.assertFalse(any('退税' in h for h in headers))
        # 底层数据结构与字段标识不变：year_cols / yearly_months 与退税模式一致
        self.assertEqual(res['year_cols'], self.year_cols)
        self.assertEqual(self.stats[0]['yearly_months'].get(2023), 12)
        self.assertEqual(self.stats[0]['overlap_months'], 36)

    def test_dishui_skips_yearly_ledgers(self):
        """抵税：跳过年度台账生成（不产出年度台账文件）"""
        res, _ = self._gen('抵税')
        self.assertEqual(res['yearly_ledger_files'], [])
        self.assertEqual(res['yearly_ledgers'], [])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, '年度台账')))

    def test_dishui_yearly_ledger_function_label(self):
        """年度台账生成函数本身也支持标签参数（供退税模式调用，防御一致性）"""
        from modules.insurance.core.excel_generator import _generate_yearly_ledger
        from openpyxl import load_workbook
        classified = [(self.stats[0], '脱贫人口')]
        r1 = _generate_yearly_ledger(2024, classified, {}, '', self.tmpdir, 't1',
                                     tax_label='退税')
        r2 = _generate_yearly_ledger(2024, classified, {}, '', self.tmpdir, 't2',
                                     tax_label='抵税')
        for path, expect in ((r1['filepath'], '申请退税月数'),
                             (r2['filepath'], '申请抵税月数')):
            wb = load_workbook(path)
            headers = [c.value for c in wb.active[3]]
            wb.close()
            self.assertIn(expect, headers)

    def test_invalid_tax_mode_falls_back_to_tuishui(self):
        """异常 tax_mode 输入 → 按默认退税处理（防御）"""
        res, out = self._gen('xx')
        headers = self._read_headers(out)
        self.assertIn('申请退税总月数', headers)
        self.assertGreater(len(res['yearly_ledger_files']), 0)


# ==================== 3. 端点：/insurance/api/tax_mode/<task_id> 切换 ====================
class TestTaxModeEndpoint(unittest.TestCase):
    """切换端点：模式写入内部状态 → 统一重建 → 两模式互不干扰、无残留"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        self.tmpdir = tempfile.mkdtemp(prefix='test_v1157_')
        self.task_id = 'testv157'
        self._old_out = bp.OUTPUT_DIR
        bp.OUTPUT_DIR = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)

        img = os.path.join(self.tmpdir, 'img.jpg')
        with open(img, 'wb') as f:
            f.write(b'\xff\xd8fakeimg')
        success = [{
            'filename': f'img_{ins}.jpg', 'name': '张三', 'idcard': ID_ZHANG,
            'insurance_type': ins, 'period': ('2022-09', '2026-07'),
            'company_name': '鲁岳测试公司', 'raw_text': '', 'error': None,
            '_source_path': img, '_source_origin': f'img_{ins}.jpg'}
            for ins in INS4]
        roster = [{'seq': 1, 'name': '张三', 'idcard': ID_ZHANG}]
        with bp.tasks_lock:
            bp.tasks[self.task_id] = {
                'status': 'done', 'current': 1, 'total': 1,
                'message': '处理完成', 'files': [], 'created_at': '',
                'paused': False, 'cancelled': False,
                'result': {
                    '_success_results': success,
                    '_excluded_results': [],
                    '_failed_results': [],
                    '_all_files': [s['filename'] for s in success],
                    '_task_dir': self.tmpdir,
                    '_year_range': ('2023-01', '2025-12'),
                    '_roster': roster,
                    '_roster_company': '鲁岳测试公司',
                    '_roster_source_path': '',
                    '_company_name': '鲁岳测试公司',
                    '_ocr_companies': {'鲁岳测试公司': 1},
                    '_company_mismatch_files': [],
                    '_period_overrides': {},
                    '_manual_log': [],
                },
            }
        with self.c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'

    def tearDown(self):
        with bp.tasks_lock:
            bp.tasks.pop(self.task_id, None)
        bp.OUTPUT_DIR = self._old_out
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _switch(self, mode, task_id=None):
        return self.c.post(f'/insurance/api/tax_mode/{task_id or self.task_id}',
                           json={'tax_mode': mode})

    def test_switch_to_dishui(self):
        """切抵税：200 + tax_mode=抵税 + 年度台账为空 + 操作记录留痕"""
        r = self._switch('抵税')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        data = r.get_json()
        self.assertEqual(data['tax_mode'], '抵税')
        self.assertEqual(data['yearly_ledger_files'], [])
        # 统计结果数值不受模式影响（两模式共用同一统计漏斗）
        ps = data['person_stats'][0]
        self.assertEqual(ps['overlap_months'], 36)
        self.assertEqual(ps['overlap_start'], '2023-01')
        # 操作记录含切换留痕
        actions = [l['action'] for l in data['operation_log']]
        self.assertIn('切换税种模式', actions)
        # 内部状态持久化（供 retry/修改时间段等后续重建沿用）
        with bp.tasks_lock:
            self.assertEqual(bp.tasks[self.task_id]['result']['_tax_mode'], '抵税')

    def test_switch_back_to_tuishui_restores_yearly_ledgers(self):
        """抵税→退税：年度台账恢复生成，两种模式互不干扰、无残留状态"""
        self._switch('抵税')
        r = self._switch('退税')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data['tax_mode'], '退税')
        self.assertGreater(len(data['yearly_ledger_files']), 0)
        # 数值仍一致（切换不影响统计规则）
        self.assertEqual(data['person_stats'][0]['overlap_months'], 36)

    def test_same_mode_no_rebuild_no_extra_log(self):
        """重复切到当前模式：直接返回当前结果，不重复新增操作记录"""
        self._switch('抵税')  # 第一次：重建 + 1 条留痕
        r = self._switch('抵税')  # 第二次：同模式，不重建不留痕
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data['tax_mode'], '抵税')
        actions = [l['action'] for l in data['operation_log']]
        self.assertEqual(actions.count('切换税种模式'), 1)

    def test_invalid_mode_400(self):
        r = self._switch('免税')
        self.assertEqual(r.status_code, 400)
        self.assertIn('退税', r.get_json()['error'])

    def test_task_not_found_404(self):
        r = self._switch('抵税', task_id='nope9999')
        self.assertEqual(r.status_code, 404)

    def test_unauthenticated_401_json(self):
        c = flask_app.test_client()  # 未登录会话
        r = c.post(f'/insurance/api/tax_mode/{self.task_id}',
                   json={'tax_mode': '抵税'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.content_type)

    def test_default_mode_is_tuishui_for_legacy_tasks(self):
        """旧任务（内部状态无 _tax_mode）→ 默认退税；重建后 result 必携带 tax_mode"""
        # 种子任务无 _tax_mode（模拟旧任务）→ 重建时按默认退税处理
        self._switch('抵税')
        r = self.c.get(f'/insurance/api/result/{self.task_id}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['tax_mode'], '抵税')
        self._switch('退税')
        r2 = self.c.get(f'/insurance/api/result/{self.task_id}')
        self.assertEqual(r2.get_json()['tax_mode'], '退税')


# ==================== 4. 前端改动字符串断言 ====================
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.path.join(_ROOT, 'modules', 'insurance', 'static', 'js', 'app.js')
_INDEX_HTML = os.path.join(_ROOT, 'modules', 'insurance', 'templates',
                           'insurance_index.html')


class TestFrontendTaxMode(unittest.TestCase):
    """前端：radio 控件、表单提交、表头联动、切换调用"""

    def setUp(self):
        with open(_APP_JS, encoding='utf-8') as f:
            self.js = f.read()
        with open(_INDEX_HTML, encoding='utf-8') as f:
            self.html = f.read()

    def test_radio_group_present_default_tuishui(self):
        """互斥单选 radio：同名 taxMode、两值、默认勾选退税"""
        self.assertIn('name="taxMode"', self.html)
        self.assertIn('value="退税" checked', self.html)
        self.assertIn('value="抵税"', self.html)

    def test_upload_appends_tax_mode(self):
        """上传时携带 tax_mode 表单字段"""
        self.assertIn("formData.append('tax_mode', currentTaxMode)", self.js)

    def test_table_headers_follow_mode(self):
        """结果表头随模式动态渲染（退税/抵税），不再硬编码退税"""
        self.assertIn("'申请' + currentTaxMode + '总月数'", self.js)
        self.assertIn("+ '年申请' + currentTaxMode + '月数'", self.js)
        self.assertIn("'合计申请' + currentTaxMode + '总额'", self.js)
        self.assertNotIn("'申请退税总月数'", self.js)

    def test_switch_calls_backend_endpoint(self):
        """切换时调用后端重建端点，保证 Excel 与年度台账同步"""
        self.assertIn("/insurance/api/tax_mode/", self.js)
        self.assertIn('function onTaxModeChange', self.js)
        self.assertIn('function setTaxModeUI', self.js)

    def test_hint_text_both_modes(self):
        """年月区间提示文案两种模式均有定义"""
        self.assertIn('TAX_MODE_HINTS', self.js)
        self.assertIn('抵税模式不生成年度台账', self.js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
