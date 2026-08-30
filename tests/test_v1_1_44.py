# -*- coding: utf-8 -*-
"""v1.1.44 合同模块两阶段重命名测试

覆盖: plan_renames / validate_renames / execute_renames / rollback_renames
测试数据（含 GB 11643 合法校验码身份证号）:
  张三 seq1  ID=11010519491231002X（尾4位 002X）→ 命名 01-张三-002X
  李四 seq2  无身份证                → 命名 02-李四（回退规则）
  王五 seq3  ID=110105195001010014（尾4位 0014）
  王五 seq4  ID=110105195001010027（尾4位 0027）→ 与 seq3 同名 → 重名待确认
  赵六 seq5  无合同文件              → roster_missing
文件:
  01张三.jpg        → auto（张三）
  张三2.jpg         → auto（张三第二文件 → (2) 后缀）
  李四-合同.jpg     → auto（业务词剔除后匹配李四）
  王五.jpg          → duplicates（花名册同名）
  无名人.jpg        → unmatched（查无此人）
  12345.jpg         → unmatched（无法识别姓名）
"""
import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.contract.core.file_renamer import (
    plan_renames, validate_renames, execute_renames, rollback_renames,
    LOG_FILENAME, PENDING_DIR_NAME,
)

ROSTER = [
    {'seq': 1, 'name': '张三', 'idcard': '11010519491231002X'},
    {'seq': 2, 'name': '李四', 'idcard': ''},
    {'seq': 3, 'name': '王五', 'idcard': '110105195001010014'},
    {'seq': 4, 'name': '王五', 'idcard': '110105195001010027'},
    {'seq': 5, 'name': '赵六', 'idcard': '110105195001010030'},
]

FILES = ['01张三.jpg', '张三2.jpg', '李四-合同.jpg', '王五.jpg', '无名人.jpg', '12345.jpg']


class TestPlanRenames(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_v1144_')
        self.paths = []
        for name in FILES:
            fp = os.path.join(self.tmp, name)
            with open(fp, 'wb') as f:
                f.write(b'\xff\xd8fake')
            self.paths.append(fp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_structure(self):
        plan = plan_renames(self.paths, ROSTER)
        self.assertEqual(plan['total'], 6)

        # auto: 张三x2 + 李四 = 3
        self.assertEqual(len(plan['auto']), 3)
        auto_names = {item['new_name']: item['original'] for item in plan['auto']}
        self.assertIn('01-张三-002X.jpg', auto_names)
        self.assertIn('01-张三-002X(2).jpg', auto_names)  # 第二文件 (2) 后缀
        self.assertIn('02-李四.jpg', auto_names)  # 无身份证回退序号-姓名
        # 张三2.jpg 先出现则首选
        self.assertNotIn('03-王五-0014.jpg', auto_names)  # 王五是重名项

        # duplicates: 王五 1 个，含两个候选（带身份证尾4位）
        self.assertEqual(len(plan['duplicates']), 1)
        dup = plan['duplicates'][0]
        self.assertEqual(dup['original'], '王五.jpg')
        self.assertEqual(len(dup['candidates']), 2)
        cand_tails = {c['idcard_tail'] for c in dup['candidates']}
        self.assertEqual(cand_tails, {'0014', '0027'})

        # unmatched: 无名人 + 12345
        self.assertEqual(len(plan['unmatched']), 2)
        reasons = ' '.join(u['reason'] for u in plan['unmatched'])
        self.assertIn('查无此人', reasons)
        self.assertIn('无法识别', reasons)

        # roster_missing: 赵六（王五因有重名待确认文件被排除）
        self.assertEqual(plan['roster_missing'], [{'seq': 5, 'name': '赵六'}])

    def test_plan_no_side_effects(self):
        """plan 阶段不得移动/重命名任何文件"""
        plan_renames(self.paths, ROSTER)
        remaining = sorted(os.listdir(self.tmp))
        self.assertEqual(remaining, sorted(FILES))


class TestValidateRenames(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_v1144_')
        self.paths = []
        for name in FILES:
            fp = os.path.join(self.tmp, name)
            with open(fp, 'wb') as f:
                f.write(b'x')
            self.paths.append(fp)
        self.source_paths = {os.path.basename(fp): fp for fp in self.paths}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validate_ok(self):
        renames = [
            {'original': '01张三.jpg', 'new_name': '01-张三-002X.jpg'},
            {'original': '王五.jpg', 'new_name': '03-王五-0014.jpg', 'seq': 3},
        ]
        self.assertEqual(validate_renames(self.source_paths, renames), [])

    def test_validate_errors(self):
        # 不存在的原文件
        errors = validate_renames(self.source_paths, [
            {'original': '不存在.jpg', 'new_name': 'a.jpg'}])
        self.assertTrue(errors)
        # 空文件名
        errors = validate_renames(self.source_paths, [
            {'original': '01张三.jpg', 'new_name': '  '}])
        self.assertTrue(errors)
        # 非法字符
        errors = validate_renames(self.source_paths, [
            {'original': '01张三.jpg', 'new_name': 'a/b.jpg'}])
        self.assertTrue(errors)
        errors = validate_renames(self.source_paths, [
            {'original': '01张三.jpg', 'new_name': '..'}])
        self.assertTrue(errors)
        # 重复新文件名（大小写不敏感）
        errors = validate_renames(self.source_paths, [
            {'original': '01张三.jpg', 'new_name': 'A.jpg'},
            {'original': '张三2.jpg', 'new_name': 'a.jpg'}])
        self.assertTrue(errors)


class TestExecuteAndRollback(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test_v1144_')
        self.src_dir = os.path.join(self.tmp, 'src')
        self.out_dir = os.path.join(self.tmp, 'out')
        os.makedirs(self.src_dir)
        self.paths = []
        for name in FILES:
            fp = os.path.join(self.src_dir, name)
            with open(fp, 'wb') as f:
                f.write(b'\xff\xd8fake')
            self.paths.append(fp)
        self.source_paths = {os.path.basename(fp): fp for fp in self.paths}
        self.plan = plan_renames(self.paths, ROSTER)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_execute_and_rollback(self):
        # 用户确认：auto 3 项（李四改名为自定义名）+ 王五归属 seq4 + 无名人 pending
        renames = []
        for item in self.plan['auto']:
            renames.append({'original': item['original'], 'new_name': item['new_name']})
        renames.append({'original': '王五.jpg', 'new_name': '04-王五-0027.jpg', 'seq': 4})
        # 覆盖一个手动修改的新文件名
        for r in renames:
            if r['original'] == '李四-合同.jpg':
                r['new_name'] = '02-李四-手工改名.jpg'
        pending = ['无名人.jpg']

        result = execute_renames(self.source_paths, self.plan, self.out_dir,
                                 renames, pending, task_id='test01')

        # 重命名 4 个（auto 3 + 重名确认 1）
        self.assertEqual(result['matched_count'], 4)
        self.assertEqual(result['conflicts_resolved_count'], 1)
        # 待处理 = pending(无名人) + 计划未匹配(12345) = 2
        self.assertEqual(result['unmatched_count'], 2)

        # 输出目录内容校验
        out_files = sorted(os.listdir(self.out_dir))
        self.assertIn('01-张三-002X.jpg', out_files)
        self.assertIn('01-张三-002X(2).jpg', out_files)
        self.assertIn('02-李四-手工改名.jpg', out_files)
        self.assertIn('04-王五-0027.jpg', out_files)
        # 待处理文件夹
        pending_files = sorted(os.listdir(os.path.join(self.out_dir, PENDING_DIR_NAME)))
        self.assertIn('无名人.jpg', pending_files)
        self.assertIn('12345.jpg', pending_files)
        # 日志与报告
        self.assertIn(LOG_FILENAME, out_files)
        self.assertIn('失败明细报告.xlsx', pending_files)

        # 日志内容校验
        with open(os.path.join(self.out_dir, LOG_FILENAME), encoding='utf-8') as f:
            log = json.load(f)
        self.assertEqual(log['task_id'], 'test01')
        self.assertEqual(len(log['renames']), 4)
        rename_map = {r['original']: r['new_name'] for r in log['renames']}
        self.assertEqual(rename_map['王五.jpg'], '04-王五-0027.jpg')
        # person 信息回填（重名项按 seq4）
        for r in log['renames']:
            if r['original'] == '王五.jpg':
                self.assertEqual(r['person_seq'], 4)
                self.assertEqual(r['person_name'], '王五')
        # 源文件不被删除
        self.assertTrue(os.path.exists(os.path.join(self.src_dir, '01张三.jpg')))

        # ---- 回滚 ----
        rb = rollback_renames(self.out_dir)
        self.assertEqual(rb, {'reverted': 4, 'failed': 0})
        out_files = sorted(os.listdir(self.out_dir))
        # 恢复为原文件名
        for name in ['01张三.jpg', '张三2.jpg', '李四-合同.jpg', '王五.jpg']:
            self.assertIn(name, out_files)
        # 新文件名不再存在
        for name in ['01-张三-002X.jpg', '01-张三-002X(2).jpg',
                     '02-李四-手工改名.jpg', '04-王五-0027.jpg']:
            self.assertNotIn(name, out_files)
        # 待处理文件夹不受回滚影响
        pending_files = sorted(os.listdir(os.path.join(self.out_dir, PENDING_DIR_NAME)))
        self.assertIn('无名人.jpg', pending_files)

        # 日志中记录了回滚明细
        with open(os.path.join(self.out_dir, LOG_FILENAME), encoding='utf-8') as f:
            log = json.load(f)
        self.assertEqual(len(log.get('rollbacks', [])), 4)

        # 重复回滚：原文件已存在 → 全部跳过（reverted=0）
        rb2 = rollback_renames(self.out_dir)
        self.assertEqual(rb2['reverted'], 0)

    def test_rollback_no_log(self):
        os.makedirs(self.out_dir, exist_ok=True)
        rb = rollback_renames(self.out_dir)
        self.assertIn('error', rb)


class TestIdcardTail(unittest.TestCase):
    """身份证尾4位格式校验（15位/18位X/非法）"""

    def _tail(self, idcard):
        from modules.contract.core.file_renamer import _idcard_tail
        return _idcard_tail({'idcard': idcard})

    def test_valid(self):
        self.assertEqual(self._tail('11010519491231002X'), '002X')
        self.assertEqual(self._tail('110105195001010014'), '0014')
        self.assertEqual(self._tail('110105500101002'), '1002')  # 15位

    def test_invalid(self):
        self.assertEqual(self._tail(''), '')
        self.assertEqual(self._tail('1234567890'), '')
        self.assertEqual(self._tail('11010519491231002A'), '')  # 末位非法
        self.assertEqual(self._tail('1101051949123100.0'), '')  # 浮点尾巴
        self.assertEqual(self._tail(None), '')
        # 15.0 浮点清理后为 15 位数字 → 合法
        self.assertEqual(self._tail('110105500101002.0'), '1002')


if __name__ == '__main__':
    unittest.main(verbosity=2)
