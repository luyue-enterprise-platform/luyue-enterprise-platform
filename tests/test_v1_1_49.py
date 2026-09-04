# -*- coding: utf-8 -*-
"""v1.1.49 自动升级功能测试

新增功能：升级包下载完成后自动静默安装（/VERYSILENT），安装完成程序自动重启。
- POST /api/app/start_update  ：白名单校验 + 忙碌保护 + 后台线程下载→校验→安装→退出进程
- GET  /api/app/update_progress：进度查询（免登录）

测试策略：
- 本地起 HTTP 服务器托管假安装包（MZ 头），白名单/min-size/子进程/退出均打桩，
  验证完整状态机 idle → downloading → installing；
- 截断/损坏文件 → error 且绝不执行安装；
- 网络失败 → error。
"""
import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import app as flask_app


def _reset_state():
    with app_module._update_lock:
        app_module._update_state.update({
            'status': 'idle', 'percent': 0, 'downloaded': 0,
            'total': 0, 'version': '', 'error': '',
        })


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class TestStartUpdateValidation(unittest.TestCase):
    """1. 接口校验：免登录、白名单、.exe 后缀、忙碌保护"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        _reset_state()

    def tearDown(self):
        _reset_state()

    def test_progress_idle_login_free(self):
        r = self.c.get('/api/app/update_progress')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'idle')

    def test_reject_non_whitelist_url(self):
        r = self.c.post('/api/app/start_update', json={
            'download_url': 'https://evil.example.com/LY_setup_v9.exe', 'version': '9.9.9'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('白名单', r.get_json()['error'])

    def test_reject_non_exe_url(self):
        r = self.c.post('/api/app/start_update', json={
            'download_url': 'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/patch.zip'})
        self.assertEqual(r.status_code, 400)

    def test_reject_empty_url(self):
        r = self.c.post('/api/app/start_update', json={})
        self.assertEqual(r.status_code, 400)

    def test_busy_returns_409(self):
        with app_module._update_lock:
            app_module._update_state['status'] = 'downloading'
        r = self.c.post('/api/app/start_update', json={
            'download_url': 'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/LY_setup_v1.1.49.exe'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['status'], 'downloading')


class TestDoUpdateEndToEnd(unittest.TestCase):
    """2. 端到端：本地下载 → 校验 → 静默安装（打桩）→ 退出进程（打桩）"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        _reset_state()
        # 假安装包（合法 PE 头 MZ）
        self.tmpdir = tempfile.mkdtemp(prefix='test_v1149_')
        self.fake_exe = os.path.join(self.tmpdir, 'LY_setup_test.exe')
        with open(self.fake_exe, 'wb') as f:
            f.write(b'MZ' + b'\x00' * 4096)
        # 本地 HTTP 服务器
        handler = functools.partial(_QuietHandler, directory=self.tmpdir)
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.port}/LY_setup_test.exe'
        # 打桩：白名单放行本地服务器、降低体积门槛、拦截子进程与退出
        self._old_prefixes = app_module._UPDATE_ALLOWED_PREFIXES
        self._old_min = app_module._UPDATE_MIN_SIZE
        app_module._UPDATE_ALLOWED_PREFIXES = (f'http://127.0.0.1:{self.port}/',)
        app_module._UPDATE_MIN_SIZE = 100
        self.popen_mock = mock.MagicMock()
        self.exit_mock = mock.MagicMock()
        self._patchers = [
            mock.patch('subprocess.Popen', self.popen_mock),
            mock.patch('os._exit', self.exit_mock),
            mock.patch('time.sleep', lambda s: None),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        # 先等后台升级线程跑完，再拆桩（否则线程会在拆桩后调用真实 Popen/os._exit）
        self._wait_done(timeout=15)
        for p in self._patchers:
            p.stop()
        app_module._UPDATE_ALLOWED_PREFIXES = self._old_prefixes
        app_module._UPDATE_MIN_SIZE = self._old_min
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_state()

    def _wait_status(self, targets, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app_module._update_lock:
                st = app_module._update_state['status']
            if st in targets:
                return st
            time.sleep(0.1)
        self.fail(f'状态未在 {timeout}s 内进入 {targets}，当前: {st}')

    def _wait_done(self, timeout=15):
        """等待后台线程结束：error 状态，或已走到退出进程（exit_mock 被调用）"""
        if self.exit_mock.called:
            return
        with app_module._update_lock:
            if app_module._update_state['status'] in ('idle', 'error'):
                return
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app_module._update_lock:
                st = app_module._update_state['status']
            if st == 'error' or self.exit_mock.called:
                return
            time.sleep(0.1)
        self.fail(f'后台升级线程未在 {timeout}s 内结束，当前: {st}')

    def test_success_flow_download_then_install(self):
        r = self.c.post('/api/app/start_update', json={
            'download_url': self.url, 'version': '1.1.49'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])

        st = self._wait_status(('installing', 'error'))
        self.assertEqual(st, 'installing', app_module._update_state.get('error'))
        self._wait_done()  # 等线程走完 Popen → os._exit（打桩），再断言

        # 进度终值
        with app_module._update_lock:
            self.assertEqual(app_module._update_state['percent'], 100)
            self.assertEqual(app_module._update_state['downloaded'], 4098)
            self.assertEqual(app_module._update_state['version'], '1.1.49')

        # 安装器以静默参数拉起
        self.exit_mock.assert_called_once_with(0)
        self.popen_mock.assert_called_once()
        args = self.popen_mock.call_args[0][0]
        self.assertTrue(args[0].endswith('.exe'))
        for flag in ('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'):
            self.assertIn(flag, args)
        # 清理下载的临时文件
        try:
            os.remove(args[0])
        except OSError:
            pass

    def test_progress_visible_during_download(self):
        r = self.c.post('/api/app/start_update', json={
            'download_url': self.url, 'version': '1.1.49'})
        self.assertEqual(r.status_code, 200)
        self._wait_status(('installing', 'error'))
        pr = self.c.get('/api/app/update_progress').get_json()
        self.assertIn(pr['status'], ('installing', 'error'))
        self.assertGreaterEqual(pr['downloaded'], 0)
        self._wait_done()

    def test_truncated_file_rejected_never_installs(self):
        # 体积门槛抬高到文件实际大小之上 → 校验失败
        app_module._UPDATE_MIN_SIZE = 1024 * 1024
        r = self.c.post('/api/app/start_update', json={
            'download_url': self.url, 'version': '1.1.49'})
        self.assertEqual(r.status_code, 200)
        st = self._wait_status(('error', 'installing'))
        self.assertEqual(st, 'error')
        self.assertIn('校验失败', app_module._update_state['error'])
        self.popen_mock.assert_not_called()
        self.exit_mock.assert_not_called()

    def test_network_failure_goes_error(self):
        # 白名单放行一个无服务端口 → 连接被拒
        app_module._UPDATE_ALLOWED_PREFIXES = ('http://127.0.0.1:1/',)
        r = self.c.post('/api/app/start_update', json={
            'download_url': 'http://127.0.0.1:1/LY_setup_test.exe', 'version': '1.1.49'})
        self.assertEqual(r.status_code, 200)
        st = self._wait_status(('error', 'installing'))
        self.assertEqual(st, 'error')
        self.popen_mock.assert_not_called()
        self.exit_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
