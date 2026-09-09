# -*- coding: utf-8 -*-
"""v2.3.1 社保模块统计与重命名四项需求测试

需求1: 花名册作为统计表唯一数据基准——姓名+身份证号与花名册严格比对，
       花名册外/无身份证号记录剔除统计（不进统计表、不进汇总合计），
       图片保留原文件名归入"异常图片"
需求2: 年度台账序号按行重新排列（1..N），不沿用花名册原始序号
需求3: 身份证号为统计与重命名的唯一匹配标识（姓名不做兜底/模糊匹配，
       防重名错配；姓名以花名册登记为准）
需求4: 合同模块同一人多文件命名全角编号（1）（2）（3），首张必带（1），
       仅一张不带编号

身份证号（GB 11643 合法校验码）:
  张三 ID=11010519491231002X  花名册 seq=1
  李四 ID=110105195001010012  花名册 seq=5（故意留空号断档，验证需求2重排）
  王五 ID=110105195001010020  花名册 seq=2（与张三同姓不同人）
  王五 ID=110105195001010039  花名册 seq=3（重名，另一身份证号）
"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance.core.roster_parser import (build_strict_roster_index,
                                                  match_record_strict)
from modules.insurance.core.file_organizer import organize_files, ABNORMAL_FOLDER
from modules.insurance.core.excel_generator import generate_excel
from modules.contract.core.file_renamer import plan_renames

ID_ZHANG = '11010519491231002X'
ID_LI = '110105195001010012'
ID_WANG_A = '110105195001010020'
ID_WANG_B = '110105195001010039'

ROSTER = [
    {'seq': 1, 'name': '张三', 'idcard': ID_ZHANG, 'identity_type': '脱贫人口',
     'contract_periods': [], 'contract_status': 'missing',
     'contract_raw': '', 'contract_error': ''},
    {'seq': 2, 'name': '王五', 'idcard': ID_WANG_A, 'identity_type': '脱贫人口',
     'contract_periods': [], 'contract_status': 'missing',
     'contract_raw': '', 'contract_error': ''},
    {'seq': 3, 'name': '王五', 'idcard': ID_WANG_B, 'identity_type': '',
     'contract_periods': [], 'contract_status': 'missing',
     'contract_raw': '', 'contract_error': ''},
    # seq 4 空缺；李四排在最后且序号为 5（验证年度台账序号重排）
    {'seq': 5, 'name': '李四', 'idcard': ID_LI, 'identity_type': '自主就业退役士兵',
     'contract_periods': [], 'contract_status': 'missing',
     'contract_raw': '', 'contract_error': ''},
]


# ==================== 需求3: 严格匹配索引 ====================
class TestStrictRosterMatch(unittest.TestCase):

    def test_idcard_is_sole_key_when_roster_has_ids(self):
        """花名册含证号：身份证号精确匹配，姓名不参与（同名不同号防错配）"""
        idx = build_strict_roster_index(ROSTER)
        self.assertTrue(idx['has_idcard'])
        # 证号命中（即使姓名为空或错误）
        self.assertIs(match_record_strict({'name': '', 'idcard': ID_ZHANG}, idx),
                      idx['by_idcard'][ID_ZHANG])
        self.assertIs(match_record_strict({'name': '错字名', 'idcard': ID_WANG_A}, idx),
                      idx['by_idcard'][ID_WANG_A])
        # 证号不在花名册 → None
        self.assertIsNone(match_record_strict(
            {'name': '张三', 'idcard': '11010519500101003X'}, idx))
        # 无证号 → None（即使姓名精确命中，也不允许）
        self.assertIsNone(match_record_strict({'name': '张三', 'idcard': ''}, idx))

    def test_no_fuzzy_match(self):
        """模糊匹配（姓名互为子串）必须不再生效"""
        idx = build_strict_roster_index(ROSTER)
        self.assertIsNone(match_record_strict({'name': '张', 'idcard': ''}, idx))
        self.assertIsNone(match_record_strict(
            {'name': '张三', 'idcard': ID_ZHANG[:-1] + '0'}, idx))

    def test_degrade_to_name_when_roster_has_no_ids(self):
        """花名册完全无证号列 → 姓名精确且唯一才匹配"""
        roster_no_id = [{'seq': 1, 'name': '张三', 'idcard': ''},
                        {'seq': 2, 'name': '李四', 'idcard': ''}]
        idx = build_strict_roster_index(roster_no_id)
        self.assertFalse(idx['has_idcard'])
        self.assertIsNotNone(match_record_strict({'name': '张三', 'idcard': ''}, idx))
        # 重名（同姓名多条）→ 无法确定 → None
        idx_dup = build_strict_roster_index(roster_no_id + [
            {'seq': 3, 'name': '张三', 'idcard': ''}])
        self.assertIsNone(match_record_strict({'name': '张三', 'idcard': ''}, idx_dup))


# ==================== 需求1+3: 文件整理（重命名） ====================
class TestOrganizeStrictRoster(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_v231_org_')
        self.out = os.path.join(self.tmp, 'out')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _img(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(b'\xff\xd8fakeimg')
        return path

    def _rec(self, filename, name, idcard, ins='养老保险',
             period=('2023-01', '2025-12')):
        return {
            'filename': filename, 'name': name, 'idcard': idcard,
            'insurance_type': ins, 'period': period, 'company_name': '测试公司',
            'raw_text': '', 'error': None,
            '_source_path': self._img(filename), '_source_origin': filename,
        }

    def test_rename_by_idcard_with_roster_name(self):
        """证号命中 → 序号+姓名取花名册（OCR姓名有误差也以花名册为准）"""
        rec = self._rec('a.jpg', '张四', ID_ZHANG)  # OCR 姓名错一字
        result = organize_files([rec], ROSTER, self.out)
        self.assertEqual(result['organized_count'], 1)
        files = result['folder_structure']['养老保险参保证明']
        self.assertEqual(files, [f'01-张三-{ID_ZHANG}.jpg'])

    def test_outside_roster_goes_abnormal_with_original_name(self):
        """证号不在花名册 / 无证号 → 保留原文件名归入异常图片"""
        recs = [
            self._rec('outside.jpg', '路人甲', '11010519500101003X'),  # 花名册外
            self._rec('noid.jpg', '张三', ''),                          # 无证号
        ]
        result = organize_files(recs, ROSTER, self.out)
        self.assertEqual(result['organized_count'], 0)
        self.assertEqual(result['abnormal_count'], 2)
        abnormal = result['folder_structure'][ABNORMAL_FOLDER]
        self.assertIn('outside.jpg', abnormal)
        self.assertIn('noid.jpg', abnormal)
        # 正常险种文件夹为空
        self.assertEqual(result['folder_structure']['养老保险参保证明'], [])

    def test_no_roster_keeps_legacy_behavior(self):
        """无花名册：保持旧行为（按OCR姓名命名，不归异常）"""
        rec = self._rec('free.jpg', '任意人', '11010519500101003X')
        result = organize_files([rec], [], self.out)
        self.assertEqual(result['organized_count'], 1)
        self.assertEqual(result['folder_structure']['养老保险参保证明'],
                         ['任意人.jpg'])
        self.assertTrue(result['no_roster'])

    def test_duplicate_name_distinct_idcards(self):
        """重名不同证号：各自按证号匹配，序号姓名不串"""
        recs = [
            self._rec('w1.jpg', '王五', ID_WANG_A),
            self._rec('w2.jpg', '王五', ID_WANG_B),
        ]
        result = organize_files(recs, ROSTER, self.out)
        files = result['folder_structure']['养老保险参保证明']
        self.assertEqual(sorted(files),
                         [f'02-王五-{ID_WANG_A}.jpg', f'03-王五-{ID_WANG_B}.jpg'])


# ==================== 需求1+3: _rebuild_result 统计过滤 ====================
class TestRebuildStrictFilter(unittest.TestCase):

    def setUp(self):
        from modules.insurance import blueprint as bp
        self.bp = bp
        self.tmpdir = tempfile.mkdtemp(prefix='test_v231_bp_')
        self.task_id = 'testv231'
        self._old_out = bp.OUTPUT_DIR
        bp.OUTPUT_DIR = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)

    def tearDown(self):
        with self.bp.tasks_lock:
            self.bp.tasks.pop(self.task_id, None)
        self.bp.OUTPUT_DIR = self._old_out
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _img(self, name):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as f:
            f.write(b'\xff\xd8fakeimg')
        return path

    def _rec(self, filename, name, idcard, ins,
             period=('2023-01', '2025-12')):
        return {
            'filename': filename, 'name': name, 'idcard': idcard,
            'insurance_type': ins, 'period': period, 'company_name': '测试公司',
            'raw_text': '', 'error': None,
            '_source_path': self._img(filename), '_source_origin': filename,
        }

    def _seed(self, success):
        with self.bp.tasks_lock:
            self.bp.tasks[self.task_id] = {
                'status': 'done', 'current': len(success), 'total': len(success),
                'message': '处理完成', 'files': [], 'created_at': '',
                'paused': False, 'cancelled': False,
                'result': {
                    '_success_results': success,
                    '_excluded_results': [],
                    '_failed_results': [],
                    '_all_files': [r['filename'] for r in success],
                    '_task_dir': self.tmpdir,
                    '_year_range': None,
                    '_roster': ROSTER,
                    '_roster_company': '测试公司',
                    '_roster_source_path': '',
                    '_company_name': '测试公司',
                    '_ocr_companies': {'测试公司': len(success)},
                    '_company_mismatch_files': [],
                    '_period_overrides': {},
                    '_manual_log': [],
                },
            }

    def test_outside_and_noid_removed_from_stats(self):
        """花名册外/无证号：不进统计表、不进汇总；图片归异常；详情标注原因"""
        success = [
            # 张三：四险齐全（纳入）
            *[self._rec(f'z{i}.jpg', '张三', ID_ZHANG, t)
              for i, t in enumerate(['养老保险', '医疗保险', '工伤保险', '失业保险'])],
            # 李四：四险齐全（纳入，身份类型=退役士兵）
            *[self._rec(f'l{i}.jpg', '李四', ID_LI, t)
              for i, t in enumerate(['养老保险', '医疗保险', '工伤保险', '失业保险'])],
            # 花名册外人员：四险齐全但证号不在花名册（整体剔除）
            *[self._rec(f'o{i}.jpg', '路人甲', '11010519500101003X', t)
              for i, t in enumerate(['养老保险', '医疗保险', '工伤保险', '失业保险'])],
            # 无证号记录（剔除）
            self._rec('noid.jpg', '王五', '', '养老保险'),
        ]
        self._seed(success)
        result = self.bp._rebuild_result(self.task_id)

        # 统计表只剩花名册内两人（+花名册补全的王五B证号人员时间段留空）
        names = {ps['name'] + '|' + ps['idcard'] for ps in result['person_stats']}
        self.assertIn(f'张三|{ID_ZHANG}', names)
        self.assertIn(f'李四|{ID_LI}', names)
        self.assertIn(f'王五|{ID_WANG_A}', names)  # 花名册补全（时间段留空）
        self.assertNotIn(f'路人甲|11010519500101003X', names)

        # 路人甲有重叠却不进统计 → person_stats 中无 36 个月之外的第三份有效重叠
        overlap_rows = [ps for ps in result['person_stats'] if ps['has_overlap']]
        self.assertEqual(len(overlap_rows), 2)  # 仅张三、李四

        # 汇总不含路人甲：脱贫月数=张三36，退役月数=李四36（王五补全无时间段）
        # person_stats 顺序按花名册：张三(1) 王五A(2) 王五B(3) 李四(5)
        by_name = {ps['name']: ps for ps in result['person_stats']}
        self.assertEqual(by_name['张三']['overlap_months'], 36)
        self.assertEqual(by_name['李四']['overlap_months'], 36)

        # 剔除计数与详情标注
        self.assertEqual(result['roster_excluded_count'], 5)  # 路人甲4条+无证号1条
        details = result['image_details']
        excluded = [d for d in details if '剔除统计' in (d.get('error') or '')]
        self.assertEqual(len(excluded), 5)
        reasons = ' '.join(d['error'] for d in excluded)
        self.assertIn('身份证号不在花名册', reasons)
        self.assertIn('无身份证号', reasons)

        # 操作记录含花名册过滤提示
        notes = [l for l in result['operation_log'] if l['action'] == '花名册过滤']
        self.assertEqual(len(notes), 1)

        # 图片归入异常文件夹（保留原文件名）
        abnormal = result['organize_result']['folder_structure'].get(ABNORMAL_FOLDER)
        for fn in ['o0.jpg', 'o1.jpg', 'o2.jpg', 'o3.jpg', 'noid.jpg']:
            self.assertIn(fn, abnormal)

    def test_roster_name_overrides_ocr_name(self):
        """需求3：证号命中的记录，统计表姓名以花名册为准"""
        success = [self._rec(f'z{i}.jpg', '张四', ID_ZHANG, t)
                   for i, t in enumerate(['养老保险', '医疗保险', '工伤保险', '失业保险'])]
        self._seed(success)
        result = self.bp._rebuild_result(self.task_id)
        ps = result['person_stats'][0]
        self.assertEqual(ps['name'], '张三')  # 花名册姓名覆盖OCR误差名
        self.assertEqual(ps['idcard'], ID_ZHANG)
        self.assertEqual(ps['overlap_months'], 36)

    def test_no_roster_unchanged(self):
        """无花名册：不过滤、不覆盖姓名（兼容 MCP 默认链路）"""
        success = [self._rec(f'z{i}.jpg', '张三', ID_ZHANG, t)
                   for i, t in enumerate(['养老保险', '医疗保险', '工伤保险', '失业保险'])]
        with self.bp.tasks_lock:
            self.bp.tasks[self.task_id] = {
                'status': 'done', 'current': 4, 'total': 4,
                'message': '处理完成', 'files': [], 'created_at': '',
                'paused': False, 'cancelled': False,
                'result': {
                    '_success_results': success,
                    '_excluded_results': [], '_failed_results': [],
                    '_all_files': [r['filename'] for r in success],
                    '_task_dir': self.tmpdir, '_year_range': None,
                    '_roster': [], '_roster_company': '', '_roster_source_path': '',
                    '_company_name': '', '_ocr_companies': {}, 
                    '_company_mismatch_files': [], '_period_overrides': {},
                    '_manual_log': [],
                },
            }
        result = self.bp._rebuild_result(self.task_id)
        self.assertEqual(result['roster_excluded_count'], 0)
        self.assertEqual(result['person_stats'][0]['name'], '张三')
        self.assertEqual(result['person_stats'][0]['overlap_months'], 36)


# ==================== 需求2: 年度台账序号重排 ====================
class TestYearlyLedgerSeqRenumber(unittest.TestCase):

    def setUp(self):
        from modules.insurance.core.stats_calculator import calc_all_stats
        self.calc_all_stats = calc_all_stats
        self.tmpdir = tempfile.mkdtemp(prefix='test_v231_yr_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_yearly_ledger_seq_is_renumbered(self):
        """年度台账序号 1..N 重排，不沿用花名册原始序号（张三=1、李四=5 → 1、2）"""
        persons = [
            {'name': '张三', 'idcard': ID_ZHANG, 'insurances': {
                '养老保险': ('2023-01', '2025-12'), '医疗保险': ('2023-01', '2025-12'),
                '工伤保险': ('2023-01', '2025-12'), '失业保险': ('2023-01', '2025-12')}},
            {'name': '李四', 'idcard': ID_LI, 'insurances': {
                '养老保险': ('2023-01', '2025-12'), '医疗保险': ('2023-01', '2025-12'),
                '工伤保险': ('2023-01', '2025-12'), '失业保险': ('2023-01', '2025-12')}},
        ]
        ps_list, year_cols = self.calc_all_stats(persons)
        out = os.path.join(self.tmpdir, '总台账.xlsx')
        gen = generate_excel(persons, out, roster=ROSTER, company_name='测试公司',
                             stats=(ps_list, year_cols))
        self.assertEqual(len(gen['yearly_ledger_files']), len(year_cols))

        from openpyxl import load_workbook
        for yf in gen['yearly_ledger_files']:
            wb = load_workbook(yf['filepath'])
            ws = wb.active
            # 数据行从第4行起：序号列应为 1、2（重排），而非花名册的 1、5
            seqs = [ws.cell(row=r, column=1).value for r in (4, 5)]
            self.assertEqual(seqs, [1, 2],
                             f'{yf["filename"]} 年度台账序号应重排为 1..N')
            # 姓名与身份证号对应不乱
            self.assertEqual(ws.cell(row=4, column=2).value, '张三')
            self.assertEqual(ws.cell(row=5, column=2).value, '李四')
            wb.close()
        # 总台账保持花名册原始序号（1、5）
        wb = load_workbook(out)
        ws = wb.active
        self.assertEqual([ws.cell(row=3, column=1).value,
                          ws.cell(row=4, column=1).value], [1, 5])
        wb.close()


# ==================== 需求4: 合同多文件全角编号 ====================
class TestContractMultiFileNumbering(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_v231_ct_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name):
        fp = os.path.join(self.tmp, name)
        with open(fp, 'wb') as f:
            f.write(b'\xff\xd8fake')
        return fp

    def test_multi_files_numbered_from_one(self):
        """同一人多文件：全部带全角编号，首张必为（1）"""
        paths = [self._file('张三-1.jpg'), self._file('张三-2.jpg'),
                 self._file('张三-3.jpg')]
        roster = [{'seq': 1, 'name': '张三', 'idcard': ID_ZHANG}]
        plan = plan_renames(paths, roster)
        names = [item['new_name'] for item in plan['auto']]
        self.assertEqual(names, [f'01-张三-002X（1）.jpg',
                                 f'01-张三-002X（2）.jpg',
                                 f'01-张三-002X（3）.jpg'])

    def test_single_file_no_number(self):
        """同一人仅一张：不带编号"""
        paths = [self._file('张三.jpg')]
        roster = [{'seq': 1, 'name': '张三', 'idcard': ID_ZHANG}]
        plan = plan_renames(paths, roster)
        self.assertEqual([i['new_name'] for i in plan['auto']],
                         ['01-张三-002X.jpg'])

    def test_mixed_people_independent_numbering(self):
        """多人混排：各自独立编号，互不影响"""
        paths = [self._file('张三-1.jpg'), self._file('李四-1.jpg'),
                 self._file('张三-2.jpg'), self._file('李四-2.jpg'),
                 self._file('王五.jpg')]
        roster = [
            {'seq': 1, 'name': '张三', 'idcard': ID_ZHANG},
            {'seq': 2, 'name': '李四', 'idcard': ID_LI},
            {'seq': 3, 'name': '王五', 'idcard': ID_WANG_A},
        ]
        plan = plan_renames(paths, roster)
        got = {i['name']: [] for i in plan['auto']}
        for item in sorted(plan['auto'], key=lambda x: x['new_name']):
            got[item['name']].append(item['new_name'])
        self.assertEqual(got['张三'], ['01-张三-002X（1）.jpg', '01-张三-002X（2）.jpg'])
        self.assertEqual(got['李四'], ['02-李四-0012（1）.jpg', '02-李四-0012（2）.jpg'])
        self.assertEqual(got['王五'], ['03-王五-0020.jpg'])  # 单张不带编号


if __name__ == '__main__':
    unittest.main(verbosity=2)
