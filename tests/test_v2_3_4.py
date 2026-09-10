# -*- coding: utf-8 -*-
"""v2.3.4 MCP 性能埋点测试

需求覆盖：
1. /mcp/rpc 每次调用记访问日志（mcp.access）：method/tool/task/结果码/单次耗时
2. _wait_and_fetch 进入/退出各记一条耗时日志（mcp.tools）
3. serverInfo 版本号跟随 version.json（顺修写死漂移）
4. 访问日志对异常输入静默容错（绝不影响响应）
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.mcp import blueprint as mcp_bp_module
from modules.mcp.core import protocol, security, tasks
from modules.mcp.core import tools as tool_registry


class _RpcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = security._CONFIG_PATH
        security._CONFIG_PATH = os.path.join(self.tmp, 'mcp_config.json')
        self.token = security.get_token()
        # 测试隔离：访问日志不写实盘
        self._orig_env = os.environ.get('LY_MCP_NO_ACCESS_FILE')
        os.environ['LY_MCP_NO_ACCESS_FILE'] = '1'

    def tearDown(self):
        security._CONFIG_PATH = self._orig_path
        if self._orig_env is None:
            os.environ.pop('LY_MCP_NO_ACCESS_FILE', None)
        else:
            os.environ['LY_MCP_NO_ACCESS_FILE'] = self._orig_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rpc(self, payload):
        with flask_app.test_client() as c:
            return c.post('/mcp/rpc', json=payload,
                          headers={'Authorization': 'Bearer %s' % self.token})


class TestAccessLog(_RpcBase):

    def test_ping_logged_with_elapsed(self):
        with self.assertLogs('mcp.access', level='INFO') as cm:
            r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 200)
        text = '\n'.join(cm.output)
        self.assertIn('method=ping', text)
        self.assertIn('code=200', text)
        self.assertIn('ms', text)

    def test_tool_call_logged_with_task_id(self):
        with self.assertLogs('mcp.access', level='INFO') as cm:
            r = self._rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                           'params': {'name': 'get_task_status',
                                      'arguments': {'task_id': 'chk-abc'}}})
        text = '\n'.join(cm.output)
        self.assertIn('method=tools/call', text)
        self.assertIn('tool=get_task_status', text)
        self.assertIn('task=chk-abc', text)

    def test_notification_logged_as_202(self):
        with self.assertLogs('mcp.access', level='INFO') as cm:
            r = self._rpc({'jsonrpc': '2.0',
                           'method': 'notifications/initialized'})
        self.assertEqual(r.status_code, 202)
        self.assertIn('code=202', '\n'.join(cm.output))

    def test_log_access_never_raises_on_garbage(self):
        # 请求上下文外 + 非法 JSON + None 响应体：一律静默，不抛异常
        mcp_bp_module._log_access('{bad json', None, 200, False,
                                  time.monotonic())


class TestWaitTimingLog(unittest.TestCase):

    def test_wait_logs_enter_and_exit(self):
        task_id = tasks.create('insurance', params={})
        tasks.update(task_id, status='success', message='done',
                     result={'summary': 'ok', 'artifacts': []})
        try:
            with self.assertLogs('mcp.tools', level='INFO') as cm:
                text, is_error = tool_registry._wait_and_fetch(task_id, 5)
            self.assertFalse(is_error)
            body = json.loads(text)
            self.assertEqual(body['status'], 'success')
            out = '\n'.join(cm.output)
            self.assertIn('进入 Server 端等待', out)
            self.assertIn('退出等待', out)
            self.assertIn(task_id, out)
        finally:
            tasks.update(task_id, status='cancelled')


class TestServerVersion(unittest.TestCase):

    def test_server_version_follows_version_json(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'version.json'), encoding='utf-8') as f:
            expect = json.load(f)['version']
        self.assertEqual(protocol._server_version(), expect)

    def test_initialize_reports_version_json(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'version.json'), encoding='utf-8') as f:
            expect = json.load(f)['version']
        req = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
               'params': {'protocolVersion': '2025-06-18'}}
        payload, code, _ = protocol.handle_jsonrpc(json.dumps(req))
        self.assertEqual(code, 200)
        self.assertEqual(payload['result']['serverInfo']['version'], expect)


if __name__ == '__main__':
    unittest.main()
