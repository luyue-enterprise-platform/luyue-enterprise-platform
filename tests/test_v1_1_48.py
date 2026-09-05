# -*- coding: utf-8 -*-
"""v1.1.48 修改时间段 500 修复测试

Bug：api_update_period 用字符串键 'name|idcard' 写入 _period_overrides，
    而 _apply_period_overrides 按元组键 (name, idcard) 解包 →
    重建时 ValueError → Flask 500 HTML → 前端报"响应格式异常"。
    （v1.1.43 引入该功能起即存在；测试当时绕过端点直写元组键，漏网）

修复：_apply_period_overrides 兼容字符串键与元组键两种键型。

回归：新增端到端测试——通过 HTTP 端点 POST update_period 并断言
    200 + JSON + person_stats 生效（v1.1.43 测试未覆盖端点路径）。
"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.insurance import blueprint as bp

ID_ZHANG = '11010519491231002X'  # 校验码合法


def _mkimg(path):
    with open(path, 'wb') as f:
        f.write(b'\xff\xd8fakeimg')
    return path


class TestApplyPeriodOverridesKeyForms(unittest.TestCase):
    """1. _apply_period_overrides 兼容两种键型"""

    def _persons(self):
        return [{'name': '张三', 'idcard': ID_ZHANG, 'insurances': {}}]

    def test_tuple_key(self):
        persons = self._persons()
        bp._apply_period_overrides(persons, {('张三', ID_ZHANG): {'养老保险': ('2022-07', '2024-06')}})
        self.assertEqual(persons[0]['insurances']['养老保险'], ('2022-07', '2024-06'))

    def test_string_key(self):
        persons = self._persons()
        bp._apply_period_overrides(persons, {f'张三|{ID_ZHANG}': {'养老保险': ('2022-07', '2024-06')}})
        self.assertEqual(persons[0]['insurances']['养老保险'], ('2022-07', '2024-06'))

    def test_mixed_keys_no_crash(self):
        persons = self._persons()
        bp._apply_period_overrides(persons, {
            ('张三', ID_ZHANG): {'养老保险': ('2022-07', '2024-06')},
            f'张三|{ID_ZHANG}': {'失业保险': ('2023-01', '2024-12')},
        })
        self.assertEqual(persons[0]['insurances']['养老保险'], ('2022-07', '2024-06'))
        self.assertEqual(persons[0]['insurances']['失业保险'], ('2023-01', '2024-12'))

    def test_string_key_empty_idcard(self):
        persons = self._persons()
        bp._apply_period_overrides(persons, {'张三|': {'医疗保险': ('2024-01', '2024-06')}})
        self.assertEqual(persons[0]['insurances']['医疗保险'], ('2024-01', '2024-06'))


class TestUpdatePeriodEndpoint(unittest.TestCase):
    """2. 端到端：POST update_period → 200 JSON + 覆盖生效（修复前此处 500 HTML）"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        self.tmpdir = tempfile.mkdtemp(prefix='test_v1148_')
        self.task_id = 'testv148'
        # OUTPUT_DIR 指向临时目录，避免污染真实 outputs
        self._old_out = bp.OUTPUT_DIR
        bp.OUTPUT_DIR = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)

        f1 = _mkimg(os.path.join(self.tmpdir, 'img_张三.jpg'))
        success = [{
            'filename': 'img_张三.jpg', 'name': '张三', 'idcard': ID_ZHANG,
            'insurance_type': '养老保险', 'period': ('2023-01', '2024-06'),
            'company_name': '鲁岳测试公司', 'raw_text': '', 'error': None,
            '_source_path': f1, '_source_origin': 'img_张三.jpg'}]
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
                    '_all_files': ['img_张三.jpg'],
                    '_task_dir': self.tmpdir,
                    '_year_range': None,
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

    def test_update_period_200_json_and_applied(self):
        r = self.c.post(f'/insurance/api/update_period/{self.task_id}', json={
            'name': '张三', 'idcard': ID_ZHANG,
            'periods': [{'insurance_type': '养老保险', 'start': '2022-07', 'end': '2024-06'}],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertIn('application/json', r.content_type)
        data = r.get_json()
        ps = [p for p in data['person_stats'] if p['name'] == '张三'][0]
        self.assertEqual(ps['insurances']['养老保险']['start'], '2022-07')
        self.assertEqual(ps['insurances']['养老保险']['end'], '2024-06')
        # 操作记录已产生（v1.1.53 起：花名册人员合同缺失会追加一条"合同比对"待补提示）
        actions = [l['action'] for l in data['operation_log']]
        self.assertEqual(actions.count('修改时间段'), 1)
        self.assertEqual(actions.count('合同比对'), 1)

    def test_update_period_then_clear(self):
        # 先修改
        self.c.post(f'/insurance/api/update_period/{self.task_id}', json={
            'name': '张三', 'idcard': ID_ZHANG,
            'periods': [{'insurance_type': '养老保险', 'start': '2022-07', 'end': '2024-06'}],
        })
        # 双空清除 → 恢复 OCR 识别值
        r = self.c.post(f'/insurance/api/update_period/{self.task_id}', json={
            'name': '张三', 'idcard': ID_ZHANG,
            'periods': [{'insurance_type': '养老保险', 'start': '', 'end': ''}],
        })
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        ps = [p for p in data['person_stats'] if p['name'] == '张三'][0]
        self.assertEqual(ps['insurances']['养老保险']['start'], '2023-01')
        self.assertEqual(ps['insurances']['养老保险']['end'], '2024-06')

    def test_update_period_validation_400(self):
        r = self.c.post(f'/insurance/api/update_period/{self.task_id}', json={
            'name': '张三', 'idcard': ID_ZHANG,
            'periods': [{'insurance_type': '养老保险', 'start': '2025-01', 'end': '2024-06'}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn('不能晚于', r.get_json()['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
