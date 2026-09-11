# -*- coding: utf-8 -*-
"""v2.4.1 MCP 防重复提交 + 超时转轮询提示测试

背景（2026-09-11 生产事故）：30 张社保任务实际耗时 687s > wait 600s，
调用方 AI 收到 isError 误判失败，重新走"预览+确认"产生第二个相同任务，
两任务并发 → onnxruntime 线程超订 → 22.9s/张（单跑 13.2s/张）恶性循环。

v2.4.1 收口：
1. 执行指纹去重：同参数任务正在执行时，确认被拦截并指向既有任务
   （三个能力：insurance / pdf2word / contract）。
2. 等待超时改非错误返回：still_running + do_not_resubmit + 轮询指引，
   不再以 isError 形态诱发调用方重试。
3. get_task_status 的 stale 提示不再建议"重新提交"。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mcp.core import adapters, artifacts, tasks
from modules.mcp.core import tools as tool_registry


class _Base(unittest.TestCase):
    """临时文件 + 任务表/指纹表隔离"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ly_v241_')
        self._orig_a = artifacts.data_dir
        self._orig_b = adapters.data_dir
        artifacts.data_dir = lambda: self.tmp
        adapters.data_dir = lambda: self.tmp
        # 真实文件（_expand 校验存在性与扩展名）
        self.f1 = self._mk('a1.png')
        self.f2 = self._mk('a2.jpg')

    def tearDown(self):
        artifacts.data_dir = self._orig_a
        adapters.data_dir = self._orig_b
        with tasks._LOCK:
            tasks._TASKS.clear()
            tasks._FINGERPRINTS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, name, ext=None):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as f:
            f.write(b'X' * 16)
        return p

    @staticmethod
    def _payload(text):
        return json.loads(text)


class TestTimeoutNonError(_Base):
    """超时降级：非错误 envelope + still_running + 勿重提指令"""

    def _mk_processing(self):
        tid = tasks.create('insurance', {'file_count': 2})
        tasks.update(tid, status='processing', total=30, current=12,
                     message='识别进行中')
        return tid

    def test_timeout_returns_ok_with_still_running(self):
        tid = self._mk_processing()
        text, is_error = tool_registry._wait_and_fetch_inner(tid, 0)
        self.assertFalse(is_error, '超时不得再以 isError 返回（诱发 AI 重试）')
        p = self._payload(text)
        self.assertEqual(p['status'], 'still_running')
        self.assertTrue(p['do_not_resubmit'])
        self.assertEqual(p['task_id'], tid)
        self.assertIn('get_task_status', p['how_to_poll'])
        self.assertIn('切勿重新提交', p['how_to_poll'])

    def test_timeout_carries_progress(self):
        tid = self._mk_processing()
        p = self._payload(tool_registry._wait_and_fetch_inner(tid, 0)[0])
        self.assertEqual(p['total'], 30)
        self.assertEqual(p['current'], 12)

    def test_error_and_success_paths_unchanged(self):
        # 失败仍走错误（真失败才报错）
        tid = tasks.create('insurance')
        tasks.fail(tid, '模拟失败')
        text, is_error = tool_registry._wait_and_fetch_inner(tid, 0)
        self.assertTrue(is_error)
        # 成功仍一次性瘦返回
        tid2 = tasks.create('insurance')
        tasks.finish(tid2, {'summary': {'ok': 1}, 'artifacts': []})
        text2, is_error2 = tool_registry._wait_and_fetch_inner(tid2, 0)
        self.assertFalse(is_error2)
        self.assertEqual(self._payload(text2)['status'], 'success')


def _fake_preview_insurance(task_id, file_paths, province, tax_mode='退税',
                            roster_path=None, year_range=None):
    tasks.update(task_id, status='waiting_confirm', total=len(file_paths),
                 message='核算预览已生成（测试桩）',
                 pending_insurance={
                     'file_paths': list(file_paths), 'province': province,
                     'tax_mode': tax_mode, 'roster_path': roster_path,
                     'year_range': tuple(year_range) if year_range else None})
    return {'effective_params': {'province': province},
            'inventory': {'total': len(file_paths)},
            'roster': {'person_count': 0}}


def _fake_confirm_insurance(task_id, overrides=None):
    tasks.update(task_id, status='processing', message='识别进行中（测试桩）')


class TestInsuranceDedup(_Base):

    def _preview(self, files=None):
        args = {'file_paths': files or [self.f1, self.f2], 'province': '610000'}
        with mock.patch.object(adapters, 'preview_insurance',
                               _fake_preview_insurance):
            text, _ = tool_registry.tool_insurance_calculate(args)
        return self._payload(text)['task_id']

    def test_same_params_confirm_blocked(self):
        """同参数任务执行中：第二次确认被拦截，不重复执行"""
        a = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance) as m:
            out, is_err = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': a})
            m.assert_called_once()
            self.assertFalse(is_err)
        # 任务 A 进入 processing；新预览 B（同参数）再确认 → 拦截
        b = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance) as m:
            out, is_err = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': b})
            m.assert_not_called()   # 绝不重复执行
        self.assertFalse(is_err)
        p = self._payload(out)
        self.assertTrue(p.get('duplicate_submission_blocked'))
        self.assertEqual(p['task_id'], a)          # 指向既有任务
        self.assertEqual(p['blocked_confirm_id'], b)
        self.assertIn('切勿重复提交', p['hint'])

    def test_different_files_not_blocked(self):
        """不同文件集 ≠ 同指纹：照常执行"""
        a = self._preview([self.f1])
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance):
            tool_registry.tool_insurance_calculate({'confirm_task_id': a})
        b = self._preview([self.f2])
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance) as m:
            out, is_err = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': b})
            m.assert_called_once()
        self.assertFalse(is_err)
        self.assertFalse(self._payload(out).get('duplicate_submission_blocked'))

    def test_terminal_task_frees_fingerprint(self):
        """既有任务到达终态后，同参数可再次执行（指纹自动清除）"""
        a = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance):
            tool_registry.tool_insurance_calculate({'confirm_task_id': a})
        tasks.finish(a, {'summary': {'done': 1}, 'artifacts': []})
        b = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance) as m:
            out, _ = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': b})
            m.assert_called_once()   # 已完成 → 允许重跑
        self.assertFalse(self._payload(out).get('duplicate_submission_blocked'))

    def test_duplicate_with_wait_attaches_to_existing(self):
        """拦截时带 wait_seconds>0：转挂既有任务等待（非错误）"""
        a = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance):
            tool_registry.tool_insurance_calculate({'confirm_task_id': a})
        b = self._preview()
        with mock.patch.object(adapters, 'confirm_insurance',
                               side_effect=_fake_confirm_insurance) as m:
            out, is_err = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': b, 'wait_seconds': 1})
            m.assert_not_called()
        self.assertFalse(is_err)
        p = self._payload(out)
        self.assertEqual(p['task_id'], a)
        self.assertEqual(p['status'], 'still_running')   # 挂到 A 上等待并超时降级


def _fake_start_pdf2word(task_id, file_paths, direction='pdf2word',
                         output_mode='individual'):
    tasks.update(task_id, status='processing', total=len(file_paths),
                 message='转换执行中（测试桩）')


class TestPdf2WordDedup(_Base):

    def _preview(self, direction='pdf2word'):
        ext = '.pdf' if direction == 'pdf2word' else '.docx'
        f = self._mk('doc' + ext)
        tool = tool_registry.tool_convert_pdf_to_word \
            if direction == 'pdf2word' else tool_registry.tool_convert_to_pdf
        text, _ = tool({'file_paths': [f]})
        return self._payload(text)['task_id']

    def test_same_batch_confirm_blocked(self):
        a = self._preview()
        with mock.patch.object(adapters, 'start_pdf2word',
                               side_effect=_fake_start_pdf2word) as m:
            tool_registry.tool_convert_pdf_to_word({'confirm_task_id': a})
            m.assert_called_once()
        b = self._preview()
        with mock.patch.object(adapters, 'start_pdf2word',
                               side_effect=_fake_start_pdf2word) as m:
            out, is_err = tool_registry.tool_convert_pdf_to_word(
                {'confirm_task_id': b})
            m.assert_not_called()
        self.assertFalse(is_err)
        p = self._payload(out)
        self.assertTrue(p.get('duplicate_submission_blocked'))
        self.assertEqual(p['task_id'], a)

    def test_different_direction_not_blocked(self):
        """pdf2word 与 topdf 方向不同 → 指纹不同"""
        a = self._preview('pdf2word')
        with mock.patch.object(adapters, 'start_pdf2word',
                               side_effect=_fake_start_pdf2word):
            tool_registry.tool_convert_pdf_to_word({'confirm_task_id': a})
        b = self._preview('topdf')
        with mock.patch.object(adapters, 'start_pdf2word',
                               side_effect=_fake_start_pdf2word) as m:
            out, _ = tool_registry.tool_convert_to_pdf(
                {'confirm_task_id': b, 'output_mode': 'merge'})
            m.assert_called_once()
        self.assertFalse(self._payload(out).get('duplicate_submission_blocked'))


def _fake_confirm_contract(task_id, choices=None):
    tasks.update(task_id, status='processing', message='重命名执行中（测试桩）')


class TestContractDedup(_Base):

    def _preview(self):
        def _fake_plan(task_id, file_paths, roster_path=None):
            tasks.update(task_id, status='waiting_confirm', total=len(file_paths),
                         message='计划已生成（测试桩）',
                         pending_contract={'file_paths': list(file_paths),
                                           'roster_path': roster_path,
                                           'plan': {'total': len(file_paths)}})
            return {'total': len(file_paths), 'auto': [], 'duplicates': [],
                    'unmatched': [], 'roster_missing': []}
        roster = self._mk('roster.xlsx')
        with mock.patch.object(adapters, 'preview_contract', _fake_plan):
            text, _ = tool_registry.tool_contract_organize(
                {'file_paths': [self.f1, self.f2], 'roster_path': roster})
        return self._payload(text)['task_id']

    def test_same_batch_confirm_blocked(self):
        a = self._preview()
        with mock.patch.object(adapters, 'confirm_contract',
                               side_effect=_fake_confirm_contract) as m:
            tool_registry.tool_contract_organize({'confirm_task_id': a})
            m.assert_called_once()
        b = self._preview()
        with mock.patch.object(adapters, 'confirm_contract',
                               side_effect=_fake_confirm_contract) as m:
            out, is_err = tool_registry.tool_contract_organize(
                {'confirm_task_id': b})
            m.assert_not_called()
        self.assertFalse(is_err)
        p = self._payload(out)
        self.assertTrue(p.get('duplicate_submission_blocked'))
        self.assertEqual(p['task_id'], a)


class TestFingerprintRegistry(_Base):

    def test_find_active_self_heals_on_terminal(self):
        fp = tool_registry._exec_fingerprint('insurance', [self.f1])
        tid = tasks.create('insurance')
        tasks.register_fingerprint(fp, tid)
        self.assertEqual(tasks.find_active_by_fingerprint(fp), tid)
        tasks.fail(tid, 'boom')
        self.assertIsNone(tasks.find_active_by_fingerprint(fp))

    def test_find_active_ignores_waiting_confirm(self):
        fp = tool_registry._exec_fingerprint('insurance', [self.f1])
        tid = tasks.create('insurance')
        tasks.update(tid, status='waiting_confirm')
        tasks.register_fingerprint(fp, tid)
        self.assertIsNone(tasks.find_active_by_fingerprint(fp))

    def test_fingerprint_order_insensitive(self):
        """文件顺序不同 = 同一批文件 = 同指纹"""
        fp1 = tool_registry._exec_fingerprint('insurance', [self.f1, self.f2])
        fp2 = tool_registry._exec_fingerprint('insurance', [self.f2, self.f1])
        self.assertEqual(fp1, fp2)


class TestStaleHint(_Base):

    def test_stale_hint_discourages_resubmit(self):
        tid = tasks.create('insurance')
        tasks.update(tid, status='processing', total=30, current=5)
        # 把创建时间拨回 1 小时前 → stale
        with tasks._LOCK:
            tasks._TASKS[tid]['created_at'] = (
                datetime.now() - timedelta(hours=1)).isoformat(timespec='seconds')
        text, _ = tool_registry.tool_get_task_status({'task_id': tid})
        p = self._payload(text)
        self.assertTrue(p.get('stale'))
        self.assertIn('不要重复提交', p.get('stale_hint', ''))
        self.assertNotIn('或重新提交', p.get('stale_hint', ''))


if __name__ == '__main__':
    unittest.main()
