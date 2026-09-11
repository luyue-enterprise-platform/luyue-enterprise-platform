# -*- coding: utf-8 -*-
"""v2.3.8 MCP 强制两阶段确认测试

背景（2026-09-11 用户反馈确立）：调用方 AI 经 MCP 调用社保核算/合同整理时，
没有把平台上需要选择的内容（省份/税种模式/统计年月/花名册/重名归属）呈现给
用户，也没有让用户确认就直接执行了。

v2.3.8 收口：
1. insurance_calculate / contract_organize 首次调用一律只做预览/生成计划，
   不存在任何直接执行分支；执行入口只有 confirm_task_id。
2. 预览响应携带 user_confirmation_required=True，how_to_confirm 明确要求
   先呈现给用户、经用户确认后才能调用确认接口。
3. 缺参响应的 suggestion 要求候选值交由用户选择，不得由调用方代替决定。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mcp.core import adapters, artifacts, tasks
from modules.mcp.core import tools as tool_registry


class _Base(unittest.TestCase):
    """构造临时参保证明目录 + 花名册（artifacts 落盘隔离到临时目录）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_a = artifacts.data_dir
        self._orig_b = adapters.data_dir
        artifacts.data_dir = lambda: self.tmp
        adapters.data_dir = lambda: self.tmp
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
        ])

    def tearDown(self):
        artifacts.data_dir = self._orig_a
        adapters.data_dir = self._orig_b
        with tasks._LOCK:
            for tid in list(tasks._TASKS):
                tasks._TASKS.pop(tid, None)
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

    def _insurance_args(self, **over):
        args = {'file_paths': [self.proofs_dir], 'province': '610000',
                'roster_path': self.roster}
        args.update(over)
        return args

    def _contract_args(self, **over):
        args = {'file_paths': [self.proofs_dir], 'roster_path': self.roster}
        args.update(over)
        return args


class TestInsuranceForcedPreview(_Base):

    def test_first_call_never_executes(self):
        """首次调用（不传 preview_only）绝不启动识别，只返回预览"""
        with mock.patch.object(adapters, 'start_insurance') as m:
            text, is_error = tool_registry.tool_insurance_calculate(
                self._insurance_args())
            m.assert_not_called()
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'waiting_confirm')
        self.assertTrue(payload.get('user_confirmation_required'))

    def test_preview_only_false_still_previews(self):
        """显式传 preview_only=false 也只预览（参数仅为兼容保留）"""
        with mock.patch.object(adapters, 'start_insurance') as m:
            text, is_error = tool_registry.tool_insurance_calculate(
                self._insurance_args(preview_only=False))
            m.assert_not_called()
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text)['status'], 'waiting_confirm')

    def test_execution_only_via_confirm(self):
        """唯一执行入口：confirm_task_id"""
        text, _ = tool_registry.tool_insurance_calculate(self._insurance_args())
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_insurance') as m:
            out, is_error = tool_registry.tool_insurance_calculate(
                {'confirm_task_id': tid})
            m.assert_called_once()
        self.assertFalse(is_error)

    def test_preview_demands_user_confirmation(self):
        """预览响应明确要求先呈现给用户、经用户确认后才能执行"""
        text, _ = tool_registry.tool_insurance_calculate(self._insurance_args())
        payload = json.loads(text)
        guide = payload.get('how_to_confirm', '')
        self.assertIn('呈现给用户', guide)
        self.assertIn('不得未经用户确认直接调用', guide)

    def test_no_direct_execution_text_remains(self):
        """工具描述不再宣称「直接执行（默认）」/「无人值守」"""
        desc = tool_registry.TOOLS['insurance_calculate']['description']
        self.assertNotIn('直接执行（默认）', desc)
        self.assertIn('强制两阶段确认', desc)


class TestContractForcedPreview(_Base):

    def test_first_call_never_executes(self):
        """首次调用绝不执行重命名，只返回重命名计划"""
        with mock.patch.object(adapters, 'start_contract') as m:
            text, is_error = tool_registry.tool_contract_organize(
                self._contract_args())
            m.assert_not_called()
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'waiting_confirm')
        self.assertTrue(payload.get('user_confirmation_required'))
        self.assertIn('roster_path', payload.get('effective_params', {}))

    def test_preview_only_false_still_previews(self):
        with mock.patch.object(adapters, 'start_contract') as m:
            text, is_error = tool_registry.tool_contract_organize(
                self._contract_args(preview_only=False))
            m.assert_not_called()
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text)['status'], 'waiting_confirm')

    def test_preview_demands_user_confirmation(self):
        text, _ = tool_registry.tool_contract_organize(self._contract_args())
        guide = json.loads(text).get('how_to_confirm', '')
        self.assertIn('呈现给用户', guide)
        self.assertIn('不得未经用户确认直接调用', guide)

    def test_no_unattended_text_remains(self):
        desc = tool_registry.TOOLS['contract_organize']['description']
        self.assertNotIn('无人值守', desc)
        self.assertIn('强制两阶段确认', desc)


class TestMissingPayloadUserChoice(_Base):
    """缺参响应：候选值须交由用户选择，不得由调用方代替决定"""

    def test_suggestion_demands_user_choice(self):
        text, is_error = tool_registry.tool_insurance_calculate(
            {'file_paths': [self.proofs_dir]})
        self.assertTrue(is_error)
        payload = json.loads(text)
        self.assertIn('不得由调用方代替用户决定', payload.get('suggestion', ''))
        prov = [i for i in payload['blocking'] if i['field'] == 'province']
        self.assertTrue(prov and prov[0].get('candidates'))


class TestSchemaForcedTwoStage(unittest.TestCase):

    def test_preview_only_deprecated_wording(self):
        for name in ('insurance_calculate', 'contract_organize'):
            schema = tool_registry.TOOLS[name]['inputSchema']
            desc = schema['properties']['preview_only']['description']
            self.assertIn('兼容保留', desc, name)
            confirm_desc = schema['properties']['confirm_task_id']['description']
            self.assertIn('用户', confirm_desc, name)


class TestPdf2WordForcedPreview(_Base):
    """v2.3.8 转换工具强制两阶段：首次调用恒为预览，confirm_task_id 唯一执行入口"""

    def setUp(self):
        super().setUp()
        self.pdf_a = os.path.join(self.tmp, '文件A.pdf')
        self.pdf_b = os.path.join(self.tmp, '文件B.pdf')
        for p in (self.pdf_a, self.pdf_b):
            with open(p, 'wb') as f:
                f.write(b'%PDF-1.4\n')

    def test_first_call_never_executes(self):
        """首次调用绝不执行转换，只返回文件清单预览"""
        with mock.patch.object(adapters, 'start_pdf2word') as m:
            text, is_error = tool_registry.tool_convert_pdf_to_word(
                {'file_paths': [self.pdf_a, self.pdf_b]})
            m.assert_not_called()
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'waiting_confirm')
        self.assertTrue(payload.get('user_confirmation_required'))
        self.assertEqual(payload['effective_params']['file_count'], 2)
        self.assertEqual(payload['inventory']['total'], 2)

    def test_topdf_preview_shows_output_mode(self):
        """转PDF预览须内联输出方式（individual/merge 属用户选择项）"""
        with mock.patch.object(adapters, 'start_pdf2word'):
            text, _ = tool_registry.tool_convert_to_pdf(
                {'file_paths': [self.pdf_a], 'output_mode': 'merge'})
        payload = json.loads(text)
        self.assertEqual(payload['effective_params']['output_mode'], 'merge')
        self.assertIn('output_mode', payload['how_to_confirm'])

    def test_preview_demands_user_confirmation(self):
        text, _ = tool_registry.tool_convert_pdf_to_word(
            {'file_paths': [self.pdf_a]})
        guide = json.loads(text).get('how_to_confirm', '')
        self.assertIn('呈现给用户', guide)
        self.assertIn('不得未经用户确认直接调用', guide)

    def test_execution_only_via_confirm(self):
        """唯一执行入口：confirm_task_id，按预览暂存清单执行"""
        text, _ = tool_registry.tool_convert_pdf_to_word(
            {'file_paths': [self.pdf_a, self.pdf_b]})
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_pdf2word') as m:
            out, is_error = tool_registry.tool_convert_pdf_to_word(
                {'confirm_task_id': tid})
            m.assert_called_once()
            self.assertEqual(m.call_args[0][1], [self.pdf_a, self.pdf_b])
            self.assertEqual(m.call_args[1]['direction'], 'pdf2word')
        self.assertFalse(is_error)

    def test_topdf_confirm_overrides_output_mode(self):
        """确认时可覆盖输出方式；非法值拒绝并给出 how_to_fix"""
        text, _ = tool_registry.tool_convert_to_pdf(
            {'file_paths': [self.pdf_a]})
        tid = json.loads(text)['task_id']
        with mock.patch.object(adapters, 'start_pdf2word') as m:
            out, is_error = tool_registry.tool_convert_to_pdf(
                {'confirm_task_id': tid, 'output_mode': 'merge'})
            m.assert_called_once()
            self.assertEqual(m.call_args[1]['output_mode'], 'merge')
        self.assertFalse(is_error)

        text2, _ = tool_registry.tool_convert_to_pdf({'file_paths': [self.pdf_a]})
        tid2 = json.loads(text2)['task_id']
        with mock.patch.object(adapters, 'start_pdf2word') as m:
            out2, is_error2 = tool_registry.tool_convert_to_pdf(
                {'confirm_task_id': tid2, 'output_mode': 'both'})
            m.assert_not_called()
        self.assertTrue(is_error2)
        payload = json.loads(out2)
        fields = [b['field'] for b in payload.get('blocking', [])]
        self.assertIn('output_mode', fields)

    def test_confirm_rejects_wrong_state(self):
        """非 waiting_confirm 状态（已在执行）不可再确认"""
        text, _ = tool_registry.tool_convert_pdf_to_word(
            {'file_paths': [self.pdf_a]})
        tid = json.loads(text)['task_id']
        # 模拟确认后任务已进入执行态（真实流程由 start_pdf2word 推进状态）
        tasks.update(tid, status='processing')
        with mock.patch.object(adapters, 'start_pdf2word') as m:
            out, is_error = tool_registry.tool_convert_pdf_to_word(
                {'confirm_task_id': tid})
            m.assert_not_called()
        self.assertTrue(is_error)
        self.assertIn('waiting_confirm', out)

    def test_status_hint_for_waiting_confirm(self):
        """get_task_status 对待确认转换任务给出确认引导"""
        text, _ = tool_registry.tool_convert_pdf_to_word(
            {'file_paths': [self.pdf_a]})
        tid = json.loads(text)['task_id']
        out, _ = tool_registry.tool_get_task_status({'task_id': tid})
        self.assertIn('转换待确认', json.loads(out).get('hint', ''))

    def test_tools_schema_and_description(self):
        for name in ('convert_pdf_to_word', 'convert_to_pdf'):
            entry = tool_registry.TOOLS[name]
            self.assertIn('强制两阶段确认', entry['description'], name)
            schema = entry['inputSchema']
            self.assertIn('confirm_task_id', schema['properties'], name)
            self.assertIn('用户', schema['properties']['confirm_task_id']['description'], name)
            self.assertEqual(schema.get('required'), [], name)


class TestPdf2WordWaitHint(_Base):
    """wait_seconds 内部等待遇到待确认任务：不空等，直接返回引导"""

    def setUp(self):
        super().setUp()
        self.pdf_a = os.path.join(self.tmp, '文件A.pdf')
        with open(self.pdf_a, 'wb') as f:
            f.write(b'%PDF-1.4\n')

    def test_wait_returns_guidance_immediately(self):
        with mock.patch.object(adapters, 'start_pdf2word'):
            text, is_error = tool_registry.tool_convert_pdf_to_word(
                {'file_paths': [self.pdf_a], 'wait_seconds': 30})
        # 预览阶段秒回（waiting_confirm），不进入内部等待
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'waiting_confirm')


class TestParseOrder(unittest.TestCase):
    """上传 order 字段解析（v2.3.8 手动排序）"""

    def setUp(self):
        from modules.pdf2word import blueprint as bp
        self.bp = bp

    def test_valid_order(self):
        seq = self.bp._parse_order('["f0","p1","f1"]')
        self.assertEqual(seq, [('file', 0), ('pick', 1), ('file', 1)])

    def test_missing_or_invalid_returns_none(self):
        for raw in (None, '', 'not json', '[]', '["x0"]', '["f"]', '{"a":1}'):
            self.assertIsNone(self.bp._parse_order(raw), repr(raw))

    def test_not_list_returns_none(self):
        self.assertIsNone(self.bp._parse_order('"f0"'))
        self.assertIsNone(self.bp._parse_order('123'))


class TestUploadOrderEndpoint(unittest.TestCase):
    """端点级：order 决定统一处理顺序（文件与文件夹可交错）"""

    def setUp(self):
        from app import app as flask_app
        from modules.pdf2word import blueprint as bp
        self.bp = bp
        self.tmp = tempfile.mkdtemp()
        self._old_upload = bp.UPLOAD_DIR
        self._old_output = bp.OUTPUT_DIR
        bp.UPLOAD_DIR = os.path.join(self.tmp, 'uploads')
        bp.OUTPUT_DIR = os.path.join(self.tmp, 'outputs')
        os.makedirs(bp.UPLOAD_DIR, exist_ok=True)
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)
        self.c = flask_app.test_client()
        with self.c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'
        # 两个真实文件 + 两个文件夹选择（pick）
        self.f1 = os.path.join(self.tmp, '单文件1.txt')
        self.f2 = os.path.join(self.tmp, '单文件2.txt')
        for p, content in ((self.f1, '甲'), (self.f2, '乙')):
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(content)
        self.pick_dir = os.path.join(self.tmp, '文件夹X')
        os.makedirs(self.pick_dir)
        self.pick_file = os.path.join(self.pick_dir, '夹内文件.txt')
        with open(self.pick_file, 'w', encoding='utf-8') as fh:
            fh.write('丙')
        with bp.picked_folders_lock:
            bp.picked_folders['pickA'] = {'folder': self.pick_dir,
                                          'files': [self.pick_file]}
            bp.picked_folders['pickB'] = {'folder': self.pick_dir,
                                          'files': [self.pick_file]}

    def tearDown(self):
        self.bp.UPLOAD_DIR = self._old_upload
        self.bp.OUTPUT_DIR = self._old_output
        with self.bp.tasks_lock:
            self.bp.tasks.clear()
        with self.bp.picked_folders_lock:
            self.bp.picked_folders.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload_capture(self, order=None, pick_ids=None):
        """打桩转换线程入口后上传一次，捕获统一顺序（saved_paths）"""
        import io
        import time
        data = {
            'direction': 'topdf',
            'output_mode': 'individual',
            'files': [(io.BytesIO('甲'.encode('utf-8')), '单文件1.txt'),
                      (io.BytesIO('乙'.encode('utf-8')), '单文件2.txt')],
        }
        if pick_ids:
            data['pick_ids'] = pick_ids
        if order is not None:
            data['order'] = order

        captured = {}

        def fake(task_id, paths, out_dir, mode=None):
            captured['paths'] = list(paths)
            with self.bp.tasks_lock:
                self.bp.tasks[task_id]['status'] = 'done'

        orig = self.bp._process_task_topdf
        self.bp._process_task_topdf = fake
        try:
            resp = self.c.post('/pdf2word/api/upload', data=data,
                               content_type='multipart/form-data')
            self.assertEqual(resp.status_code, 200,
                             resp.get_data(as_text=True)[:300])
            task_id = resp.get_json()['task_id']
            deadline = time.time() + 10
            while time.time() < deadline:
                with self.bp.tasks_lock:
                    if self.bp.tasks.get(task_id, {}).get('status') == 'done':
                        break
                time.sleep(0.05)
        finally:
            self.bp._process_task_topdf = orig
        return captured.get('paths', [])

    def test_order_files_and_picks_interleaved(self):
        """order=["p0","f1","f0"]：文件夹最前、单文件2次之、单文件1最后"""
        paths = self._upload_capture(order='["p0","f1","f0"]',
                                     pick_ids=['pickA'])
        self.assertEqual(len(paths), 3)
        self.assertTrue(paths[0].endswith('夹内文件.txt'), paths)
        self.assertTrue(paths[1].endswith('单文件2.txt'), paths)
        self.assertTrue(paths[2].endswith('单文件1.txt'), paths)

    def test_order_fallback_legacy(self):
        """无 order：回退旧行为——先全部文件（提交序）、再全部文件夹"""
        paths = self._upload_capture(pick_ids=['pickA'])
        self.assertEqual(len(paths), 3)
        self.assertTrue(paths[0].endswith('单文件1.txt'), paths)
        self.assertTrue(paths[1].endswith('单文件2.txt'), paths)
        self.assertTrue(paths[2].endswith('夹内文件.txt'), paths)

    def test_invalid_order_falls_back(self):
        """非法 order 不报错，回退旧顺序"""
        paths = self._upload_capture(order='["bogus"]', pick_ids=['pickA'])
        self.assertEqual(len(paths), 3)
        self.assertTrue(paths[0].endswith('单文件1.txt'), paths)


class TestThinReturnSanitized(_Base):
    """v2.3.8 返回安全化：完成态返回不得包含台账/表格类具体数据

    用户要求：任务执行完成后的返回内容只保留——是否成功完成、失败原因、
    结果文件存放路径；敏感数据仅保留在落盘文件，不进返回内容。
    """

    def test_scrub_summary_rules(self):
        """清洗规则：标量/小型枚举保留，行级明细与嵌套结构降为计数"""
        from modules.mcp.core.tools import _scrub_summary
        scrubbed = _scrub_summary({
            'person_count': 2,                       # 标量 → 保留
            'tax_mode': '退税',                      # 标量 → 保留
            'year_cols': ['2023', '2024'],           # 小型标量列表 → 保留
            'person_stats': [{'name': 'x'}] * 200,   # 行级列表 → 计数
            'ledger_rows': [{'r': 1}],               # 行级列表 → 计数
            'nested': {'deep': [{'a': 1}]},          # 嵌套结构 → 计数
            'identity_type_dist': {'脱贫人口': 2},    # 小型计数字典 → 保留
        })
        self.assertEqual(scrubbed['person_count'], 2)
        self.assertEqual(scrubbed['year_cols'], ['2023', '2024'])
        self.assertEqual(scrubbed['identity_type_dist'], {'脱贫人口': 2})
        self.assertNotIn('person_stats', scrubbed)
        self.assertEqual(scrubbed['person_stats_count'], 200)
        self.assertNotIn('ledger_rows', scrubbed)
        self.assertEqual(scrubbed['ledger_rows_count'], 1)
        self.assertNotIn('nested', scrubbed)

    def test_completed_return_has_no_sensitive_rows(self):
        """即使上游误把明细写进 summary，完成态返回也被强制清洗"""
        tid = tasks.create('insurance', {})
        card = artifacts.make_card(
            os.path.join(self.tmp, '完整结果.json'), kind='result')
        poisoned = {
            'summary': {
                'person_count': 2,
                'year_cols': ['2023', '2024'],
                'person_stats': [{'name': '王冬',
                                  'idcard': '610527199006135611'}],
                'ledger_rows': [{'a': 1}, {'b': 2}],
                'nested': {'deep': [{'a': 1}]},
            },
            'output_dir': self.tmp,
            'artifacts': [card],
        }
        tasks.finish(tid, poisoned, '核算完成')
        text, is_error = tool_registry.tool_get_task_result({'task_id': tid})
        self.assertFalse(is_error)
        payload = json.loads(text)
        # 必要信息齐全：成功状态 + 存放路径
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['output_dir'], self.tmp)
        # 台账/表格具体数据不进返回
        s = payload['summary']
        self.assertNotIn('person_stats', s)
        self.assertEqual(s.get('person_stats_count'), 1)
        self.assertNotIn('ledger_rows', s)
        self.assertNotIn('nested', s)
        # 身份证号绝不出现（全文级检查）
        self.assertNotIn('610527199006135611', text)
        self.assertNotIn('王冬', text)

    def test_insurance_summary_drops_company_name(self):
        """社保摘要不含 company_name（公司名属台账数据，仅落盘保留）"""
        tid = tasks.create('insurance', {})
        fake_native = {
            'status': 'success',
            'result': {
                'person_count': 1, 'ocr_count': 1, 'success_count': 1,
                'company_name': '某某测试公司',
                'excel_path': os.path.join(self.tmp, '总台账.xlsx'),
                'person_stats': [{'name': '王冬'}],
            },
        }
        with mock.patch.object(adapters, 'insurance_native',
                               return_value=fake_native):
            adapters.materialize_insurance(tid)
        res = (tasks.get(tid) or {}).get('result') or {}
        summary = res.get('summary') or {}
        self.assertNotIn('company_name', summary)
        self.assertEqual(summary.get('person_count'), 1)
        # 完整结果（含 company_name）仍在落盘 JSON 中
        card = res['artifacts'][0]
        with open(card['path'], encoding='utf-8') as f:
            full = json.load(f)
        self.assertEqual(full['company_name'], '某某测试公司')

    def test_failure_return_carries_reason_and_path(self):
        """失败返回：失败原因明确，不因安全化丢失排障信息"""
        tid = tasks.create('pdf2word', {})
        tasks.fail(tid, '第3个文件转换失败: 文件损坏', '转换失败')
        with mock.patch.object(adapters, 'start_pdf2word'):
            text, is_error = tool_registry.tool_get_task_result({'task_id': tid})
        self.assertTrue(is_error)
        payload = json.loads(text)
        self.assertEqual(payload['status'], 'error')
        self.assertIn('文件损坏', payload['error'])


if __name__ == '__main__':
    unittest.main()
