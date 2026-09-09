# -*- coding: utf-8 -*-
"""MCP Blueprint 测试（v2.2.0 新增模块）

需求覆盖：
1. 模块与工具：7 个工具齐全，Schema 完整，handler 可调用
2. 协议层：JSON-RPC 2.0（initialize 版本协商 / tools.list / tools.call /
   ping / 通知 202 / 错误码 -32700 -32600 -32601 -32602）
3. 令牌与开关：自动生成、重新生成、校验、停用后拒绝服务
4. 端点：Bearer 401、停用 403、GET 405、门户页与状态接口需登录
5. 入参处理：_expand 目录递归与类型/存在性校验、_year_range 时间段校验
6. 任务表：登记、进度计算、完成/失败、对外视图
7. 适配器：pdf2word / topdf 摘要、合同整理、社保原生任务登记与省份校验
"""
import json
import os
import sys
import shutil
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.mcp import __version__ as MCP_VERSION
from modules.mcp.core import adapters, protocol, security, tasks, tools as tool_registry
from modules.mcp import blueprint as mcp_bp_module


def _login(c):
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'tester'


def _wait_task(task_id, timeout=20.0):
    """等待后台线程任务进入终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tasks.get(task_id)
        if t and t['status'] in ('success', 'error', 'cancelled'):
            return t
        time.sleep(0.05)
    return tasks.get(task_id)


def _rmtree(path):
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ==================== 1. 模块与工具清单 ====================
class TestModuleAndTools(unittest.TestCase):

    def test_module_version(self):
        self.assertEqual(MCP_VERSION, '1.0.0')

    def test_blueprint_registered(self):
        self.assertEqual(mcp_bp_module.mcp_bp.name, 'mcp')
        self.assertIn('mcp', flask_app.blueprints)
        self.assertEqual(flask_app.blueprints['mcp'].url_prefix, '/mcp')

    def test_tool_count_and_names(self):
        names = {t['name'] for t in tool_registry.list_tools()}
        self.assertEqual(names, {
            'insurance_provinces', 'insurance_calculate',
            'convert_pdf_to_word', 'convert_to_pdf',
            'contract_organize', 'get_task_status', 'get_task_result',
        })
        self.assertEqual(len(tool_registry.list_tools()), 7)

    def test_each_tool_has_schema_and_handler(self):
        for name, spec in tool_registry.TOOLS.items():
            self.assertIn('description', spec, name)
            self.assertTrue(callable(spec['handler']), name)
            schema = spec['inputSchema']
            self.assertEqual(schema['type'], 'object', name)
            self.assertIn('properties', schema, name)
            self.assertIn('required', schema, name)
        # 对外清单不含内部 handler
        for t in tool_registry.list_tools():
            self.assertNotIn('handler', t)

    def test_list_tools_json_serializable(self):
        json.dumps(tool_registry.list_tools(), ensure_ascii=False)


# ==================== 2. 协议层 ====================
class TestProtocol(unittest.TestCase):

    def _call(self, method, params=None, req_id=1):
        req = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
        if params is not None:
            req['params'] = params
        payload, code, notify = protocol.handle_jsonrpc(json.dumps(req))
        return payload, code, notify

    def test_initialize_echoes_supported_version(self):
        for v in ('2025-06-18', '2025-03-26', '2024-11-05'):
            payload, code, _ = self._call('initialize', {'protocolVersion': v})
            self.assertEqual(code, 200)
            self.assertEqual(payload['result']['protocolVersion'], v)

    def test_initialize_falls_back_on_unknown_version(self):
        payload, _code, _ = self._call('initialize', {'protocolVersion': '1999-01-01'})
        self.assertEqual(payload['result']['protocolVersion'],
                         protocol.DEFAULT_PROTOCOL_VERSION)
        self.assertEqual(payload['result']['serverInfo']['name'],
                         'luyue-enterprise-platform')
        self.assertIn('tools', payload['result']['capabilities'])

    def test_tools_list(self):
        payload, code, _ = self._call('tools/list')
        self.assertEqual(code, 200)
        self.assertEqual(len(payload['result']['tools']), 7)

    def test_ping(self):
        payload, code, _ = self._call('ping')
        self.assertEqual(code, 200)
        self.assertEqual(payload['result'], {})

    def test_notification_returns_202_without_body(self):
        raw = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        payload, code, notify = protocol.handle_jsonrpc(raw)
        self.assertTrue(notify)
        self.assertIsNone(payload)
        self.assertEqual(code, 202)

    def test_unknown_method(self):
        payload, code, _ = self._call('foo/bar')
        self.assertEqual(code, 200)
        self.assertEqual(payload['error']['code'], protocol.METHOD_NOT_FOUND)

    def test_missing_method(self):
        raw = json.dumps({'jsonrpc': '2.0', 'id': 7})
        payload, code, _ = protocol.handle_jsonrpc(raw)
        self.assertEqual(code, 400)
        self.assertEqual(payload['error']['code'], protocol.INVALID_REQUEST)

    def test_invalid_json(self):
        payload, code, notify = protocol.handle_jsonrpc('{not json')
        self.assertEqual(code, 400)
        self.assertFalse(notify)
        self.assertEqual(payload['error']['code'], protocol.PARSE_ERROR)

    def test_non_object_request(self):
        payload, code, _ = protocol.handle_jsonrpc('[1,2]')
        self.assertEqual(code, 200)
        self.assertTrue(isinstance(payload, list))

    def test_batch_request(self):
        raw = json.dumps([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ])
        payload, code, _ = protocol.handle_jsonrpc(raw)
        self.assertEqual(code, 200)
        self.assertEqual(len(payload), 2)

    def test_tools_call_missing_name(self):
        payload, _code, _ = self._call('tools/call', {'arguments': {}})
        self.assertEqual(payload['error']['code'], protocol.INVALID_PARAMS)

    def test_tools_call_bad_arguments_type(self):
        payload, _code, _ = self._call(
            'tools/call', {'name': 'insurance_provinces', 'arguments': 'x'})
        self.assertEqual(payload['error']['code'], protocol.INVALID_PARAMS)

    def test_tools_call_unknown_tool_is_tool_error(self):
        payload, code, _ = self._call('tools/call', {'name': 'nope', 'arguments': {}})
        self.assertEqual(code, 200)
        self.assertTrue(payload['result']['isError'])
        self.assertIn('未知工具', payload['result']['content'][0]['text'])

    def test_tools_call_success_shape(self):
        payload, _code, _ = self._call(
            'tools/call', {'name': 'insurance_provinces', 'arguments': {}})
        self.assertFalse(payload['result']['isError'])
        text = payload['result']['content'][0]['text']
        self.assertIn('provinces', json.loads(text))


# ==================== 3. 令牌与开关 ====================
class TestSecurity(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = security._CONFIG_PATH
        security._CONFIG_PATH = os.path.join(self.tmp, 'mcp_config.json')

    def tearDown(self):
        security._CONFIG_PATH = self._orig_path
        _rmtree(self.tmp)

    def test_token_autogenerated_and_stable(self):
        t1 = security.get_token()
        self.assertTrue(t1)
        self.assertEqual(security.get_token(), t1)

    def test_regenerate_changes_token(self):
        old = security.get_token()
        new = security.regenerate_token()
        self.assertNotEqual(old, new)
        self.assertEqual(security.get_token(), new)

    def test_verify(self):
        token = security.get_token()
        self.assertTrue(security.verify(token))
        self.assertFalse(security.verify('wrong'))
        self.assertFalse(security.verify(''))
        self.assertFalse(security.verify(None))

    def test_disabled_blocks_verify(self):
        token = security.get_token()
        security.set_enabled(False)
        self.assertFalse(security.is_enabled())
        self.assertFalse(security.verify(token))
        security.set_enabled(True)
        self.assertTrue(security.verify(token))

    def test_config_persisted(self):
        token = security.get_token()
        security.set_enabled(False)
        with open(security._CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertEqual(cfg['token'], token)
        self.assertFalse(cfg['enabled'])

    def test_missing_token_no_create(self):
        # 全新配置文件下，create=False 不应凭空生成
        self.assertIsNone(security.get_token(create=False))


# ==================== 4. MCP 端点鉴权 ====================
class TestRpcEndpoint(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = security._CONFIG_PATH
        security._CONFIG_PATH = os.path.join(self.tmp, 'mcp_config.json')
        self.token = security.get_token()

    def tearDown(self):
        security._CONFIG_PATH = self._orig_path
        _rmtree(self.tmp)

    def _rpc(self, payload, token=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = 'Bearer %s' % token
        with flask_app.test_client() as c:
            return c.post('/mcp/rpc', json=payload, headers=headers)

    def test_health_is_public(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertNotIn('token', r.get_json())

    def test_no_token_returns_401(self):
        r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_returns_401(self):
        r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, token='bad')
        self.assertEqual(r.status_code, 401)

    def test_valid_token_initialize(self):
        r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-06-18'}},
                      token=self.token)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(body['id'], 1)

    def test_valid_token_tools_list(self):
        r = self._rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                      token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()['result']['tools']), 7)

    def test_notification_returns_202_empty_body(self):
        r = self._rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                      token=self.token)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data, b'')

    def test_disabled_returns_403(self):
        security.set_enabled(False)
        try:
            r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                          token=self.token)
            self.assertEqual(r.status_code, 403)
        finally:
            security.set_enabled(True)

    def test_get_rpc_returns_405(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/rpc')
        self.assertEqual(r.status_code, 405)
        self.assertIn('endpoint', r.get_json())


# ==================== 5. 门户页与状态接口 ====================
class TestPortalEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = security._CONFIG_PATH
        security._CONFIG_PATH = os.path.join(self.tmp, 'mcp_config.json')

    def tearDown(self):
        security._CONFIG_PATH = self._orig_path
        _rmtree(self.tmp)

    def test_index_requires_login(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/')
        self.assertIn(r.status_code, (302, 401))

    def test_index_renders_for_logged_in(self):
        with flask_app.test_client() as c:
            _login(c)
            r = c.get('/mcp/')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('/mcp/rpc', html)
        self.assertIn(security.get_token(), html)

    def test_status_lists_tools(self):
        with flask_app.test_client() as c:
            _login(c)
            r = c.get('/mcp/api/status')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data['enabled'])
        self.assertEqual(len(data['tools']), 7)
        self.assertTrue(data['token'])
        self.assertTrue(data['endpoint'].endswith('/mcp/rpc'))

    def test_status_requires_login(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/api/status')
        self.assertIn(r.status_code, (302, 401))

    def test_regenerate_token_endpoint(self):
        with flask_app.test_client() as c:
            _login(c)
            old = security.get_token()
            r = c.post('/mcp/api/token/regenerate')
            self.assertEqual(r.status_code, 200)
            new = r.get_json()['token']
            self.assertNotEqual(old, new)
            self.assertEqual(security.get_token(), new)

    def test_toggle_endpoint(self):
        with flask_app.test_client() as c:
            _login(c)
            r = c.post('/mcp/api/toggle', json={'enabled': False})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()['enabled'])
            self.assertFalse(security.is_enabled())
            r2 = c.post('/mcp/api/toggle', json={'enabled': True})
            self.assertTrue(r2.get_json()['enabled'])
            self.assertTrue(security.is_enabled())


# ==================== 6. 入参处理 ====================
class TestParamHandling(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, 'sub'))
        self.a = os.path.join(self.tmp, 'a.PDF')
        self.b = os.path.join(self.tmp, 'sub', 'b.pdf')
        self.c = os.path.join(self.tmp, 'c.txt')
        for p in (self.a, self.b, self.c):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')

    def tearDown(self):
        _rmtree(self.tmp)

    def test_expand_directory_recursive(self):
        got = tool_registry._expand([self.tmp], {'.pdf'})
        self.assertEqual(len(got), 2)
        self.assertIn(self.a, got)
        self.assertIn(self.b, got)

    def test_expand_single_string_path(self):
        got = tool_registry._expand(self.a, {'.pdf'})
        self.assertEqual(got, [self.a])

    def test_expand_rejects_unsupported_type(self):
        with self.assertRaises(ValueError) as ctx:
            tool_registry._expand([self.c], {'.pdf'})
        self.assertIn('不支持的文件类型', str(ctx.exception))

    def test_expand_rejects_missing_path(self):
        with self.assertRaises(ValueError) as ctx:
            tool_registry._expand([os.path.join(self.tmp, 'nope.pdf')], {'.pdf'})
        self.assertIn('路径不存在', str(ctx.exception))

    def test_expand_rejects_empty(self):
        with self.assertRaises(ValueError):
            tool_registry._expand([], {'.pdf'})
        with self.assertRaises(ValueError):
            tool_registry._expand('', {'.pdf'})

    def test_expand_empty_dir(self):
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        with self.assertRaises(ValueError):
            tool_registry._expand([empty], {'.pdf'})

    def test_year_range_full(self):
        self.assertEqual(
            tool_registry._year_range({'year_start': 2026, 'month_start': 1,
                                       'year_end': 2026, 'month_end': 12}),
            ('2026-01', '2026-12'))

    def test_year_range_partial_is_none(self):
        self.assertIsNone(tool_registry._year_range(
            {'year_start': 2026, 'month_start': 1, 'year_end': 2026}))

    def test_year_range_invalid(self):
        with self.assertRaises(ValueError):
            tool_registry._year_range({'year_start': 'x', 'month_start': 1,
                                       'year_end': 2026, 'month_end': 12})

    def test_year_range_reversed(self):
        with self.assertRaises(ValueError) as ctx:
            tool_registry._year_range({'year_start': 2026, 'month_start': 7,
                                       'year_end': 2026, 'month_end': 3})
        self.assertIn('起始不得晚于截止', str(ctx.exception))


# ==================== 7. 任务登记表 ====================
class TestTaskRegistry(unittest.TestCase):

    def test_create_and_get(self):
        tid = tasks.create('pdf2word', {'file_count': 3})
        t = tasks.get(tid)
        self.assertEqual(t['capability'], 'pdf2word')
        self.assertEqual(t['status'], 'pending')
        self.assertTrue(tasks.exists(tid))
        self.assertFalse(tasks.exists('nosuch'))

    def test_progress_calculation(self):
        tid = tasks.create('contract', {})
        tasks.update(tid, current=1, total=4)
        self.assertEqual(tasks.get(tid)['progress'], 25)
        tasks.update(tid, current=4, total=4)
        self.assertEqual(tasks.get(tid)['progress'], 100)

    def test_finish_and_fail(self):
        tid = tasks.create('pdf2word', {})
        tasks.update(tid, total=5)
        tasks.finish(tid, {'ok': True})
        t = tasks.get(tid)
        self.assertEqual(t['status'], 'success')
        self.assertEqual(t['progress'], 100)
        self.assertEqual(t['result'], {'ok': True})

        tid2 = tasks.create('pdf2word', {})
        tasks.fail(tid2, 'boom')
        t2 = tasks.get(tid2)
        self.assertEqual(t2['status'], 'error')
        self.assertEqual(t2['error'], 'boom')

    def test_public_view_hides_internal_params(self):
        tid = tasks.create('insurance', {'secret': 1})
        view = tasks.public_view(tasks.get(tid))
        self.assertNotIn('params', view)
        self.assertNotIn('native_task_id', view)
        for k in ('task_id', 'capability', 'status', 'message'):
            self.assertIn(k, view)

    def test_update_unknown_task(self):
        self.assertIsNone(tasks.update('nosuch', status='x'))


# ==================== 8. 业务适配器 ====================
class TestAdapters(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdfs = []
        for name in ('a.pdf', 'b.pdf'):
            p = os.path.join(self.tmp, name)
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')
            self.pdfs.append(p)
        self.imgs = []
        for name in ('张三.jpg', '李四.jpg', '王五.jpg'):
            p = os.path.join(self.tmp, name)
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')
            self.imgs.append(p)

    def tearDown(self):
        _rmtree(self.tmp)

    # ---- pdf2word ----
    def test_pdf2word_summary(self):
        from modules.pdf2word.core import converter
        orig = converter.batch_convert
        seen = {}

        def fake(pdf_files, output_dir, progress_callback=None):
            seen['files'] = list(pdf_files)
            seen['out'] = output_dir
            for i, p in enumerate(pdf_files, 1):
                if progress_callback:
                    progress_callback(i, len(pdf_files), os.path.basename(p), {})
            return [{'pdf_name': os.path.basename(p),
                     'docx_name': os.path.splitext(os.path.basename(p))[0] + '.docx',
                     'ok': True, 'error': None} for p in pdf_files]

        converter.batch_convert = fake
        try:
            tid = tasks.create('pdf2word', {})
            adapters.start_pdf2word(tid, self.pdfs, direction='pdf2word')
            t = _wait_task(tid)
            self.assertEqual(t['status'], 'success')
            self.assertEqual(t['result']['direction'], 'pdf2word')
            self.assertEqual(t['result']['total'], 2)
            self.assertEqual(t['result']['success'], 2)
            self.assertEqual(t['result']['failed'], 0)
            self.assertTrue(os.path.isdir(t['result']['output_dir']))
            self.assertEqual(len(seen['files']), 2)
        finally:
            converter.batch_convert = orig

    def test_topdf_counts_skipped(self):
        from modules.pdf2word.core import to_pdf
        orig = to_pdf.batch_to_pdf

        def fake(files, output_dir, output_mode='individual',
                 progress_callback=None, merged_name='合并结果.pdf'):
            return ([{'name': 'a', 'out_name': 'a.pdf'}],
                    [{'name': 'b', 'reason': '不支持的格式'}])

        to_pdf.batch_to_pdf = fake
        try:
            tid = tasks.create('pdf2word', {})
            adapters.start_pdf2word(tid, self.imgs, direction='topdf',
                                    output_mode='merge')
            t = _wait_task(tid)
            self.assertEqual(t['status'], 'success')
            self.assertEqual(t['result']['total'], 2)
            self.assertEqual(t['result']['success'], 1)
            self.assertEqual(t['result']['failed'], 1)
            self.assertEqual(t['result']['files'][1]['error'], '不支持的格式')
        finally:
            to_pdf.batch_to_pdf = orig

    # ---- 合同整理 ----
    def test_contract_requires_roster(self):
        tid = tasks.create('contract', {})
        adapters.start_contract(tid, self.imgs, roster_path=None)
        t = _wait_task(tid)
        self.assertEqual(t['status'], 'error')
        self.assertIn('花名册', t['error'])

    def test_contract_organize_end_to_end(self):
        roster = os.path.join(self.tmp, 'roster.csv')
        with open(roster, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('序号,姓名,身份证号\n1,张三,610101199001011234\n'
                    '2,李四,\n')
        tid = tasks.create('contract', {})
        adapters.start_contract(tid, self.imgs, roster_path=roster)
        t = _wait_task(tid)
        self.assertEqual(t['status'], 'success', t.get('error'))
        res = t['result']
        self.assertEqual(res['total'], 3)
        self.assertEqual(res['renamed'], 2)      # 张三、李四命中
        self.assertEqual(res['pending'], 1)      # 王五未匹配 → 待处理
        self.assertTrue(os.path.isdir(res['output_dir']))
        self.assertTrue(os.path.isdir(os.path.join(res['output_dir'], '待处理')))

    # ---- 社保 ----
    def test_insurance_rejects_unknown_province(self):
        tid = tasks.create('insurance', {})
        with self.assertRaises(ValueError) as ctx:
            adapters.start_insurance(tid, self.imgs, '不存在的省份')
        self.assertIn('省份不支持', str(ctx.exception))
        # 未启动线程，任务仍为 pending
        self.assertEqual(tasks.get(tid)['status'], 'pending')

    def test_insurance_registers_native_task(self):
        from modules.insurance import blueprint as ins_bp
        from modules.insurance.core import template_engine
        orig = ins_bp.process_task
        calls = []

        def fake(*args):
            calls.append(args)

        ins_bp.process_task = fake
        tid = tasks.create('insurance', {})
        try:
            code = template_engine.get_provinces()[0]['province_code']
            adapters.start_insurance(tid, self.imgs, code, tax_mode='退税')
            deadline = time.time() + 5
            while time.time() < deadline and not calls:
                time.sleep(0.05)
            self.assertTrue(calls, 'process_task 未被调用')
            args = calls[0]
            self.assertEqual(args[0], tid)
            self.assertEqual(args[1], self.imgs)
            self.assertEqual(args[6], '退税')
            self.assertEqual(args[7], code)
            # 原生任务表已按宿模块结构注册
            with ins_bp.tasks_lock:
                native = ins_bp.tasks.get(tid)
            self.assertIsNotNone(native)
            self.assertEqual(native['total'], 3)
            self.assertFalse(native['cancelled'])
            self.assertEqual(tasks.get(tid)['native_task_id'], tid)
            self.assertEqual(adapters.insurance_native(tid)['total'], 3)
        finally:
            ins_bp.process_task = orig
            with ins_bp.tasks_lock:
                ins_bp.tasks.pop(tid, None)


if __name__ == '__main__':
    unittest.main()
