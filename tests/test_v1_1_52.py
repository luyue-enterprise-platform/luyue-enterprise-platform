# -*- coding: utf-8 -*-
"""v1.1.52 自动升级加固测试

背景（v1.1.51 事故）：
发布到 COS 的 v1.1.51 安装包本身损坏（Inno 编译异常，启动数秒内以退出码 1 退出，
什么文件都没装）。而 _do_update 旧逻辑 Popen 后不看安装器存活直接 os._exit，
导致用户端“下载完成 → 程序消失 → 什么都没装上”，且程序已自杀无法再次自动升级。

v1.1.52 加固：
- Popen 后在 _UPDATE_INSTALL_GRACE_SEC 秒观察窗口内轮询 proc.poll()；
- 安装器“早退”且退出码非零 → RuntimeError → status=error，当前进程存活（不自杀），
  前端提示失败并回退浏览器下载；
- 安装器存活（poll 返回 None 直到窗口结束）或正常快速完成（rc==0）→ 维持原逻辑
  sleep 后 os._exit(0) 释放 EXE 锁。

测试策略：沿用 v1.1.49 的本地 HTTP 假安装包 + 打桩（白名单/体积/Popen/os._exit/sleep），
并按“先等后台线程结束再拆桩”的铁律防竞态。
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


class _UpdateTestBase(unittest.TestCase):
    """公共脚手架：本地假安装包 + 全打桩；子类/用例按需配置 proc.poll 行为"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        _reset_state()
        self.tmpdir = tempfile.mkdtemp(prefix='test_v1152_')
        self.fake_exe = os.path.join(self.tmpdir, 'LY_setup_test.exe')
        with open(self.fake_exe, 'wb') as f:
            f.write(b'MZ' + b'\x00' * 4096)
        handler = functools.partial(_QuietHandler, directory=self.tmpdir)
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.port}/LY_setup_test.exe'
        # 打桩
        self._old_prefixes = app_module._UPDATE_ALLOWED_PREFIXES
        self._old_min = app_module._UPDATE_MIN_SIZE
        self._old_grace = app_module._UPDATE_INSTALL_GRACE_SEC
        app_module._UPDATE_ALLOWED_PREFIXES = (f'http://127.0.0.1:{self.port}/',)
        app_module._UPDATE_MIN_SIZE = 100
        app_module._UPDATE_INSTALL_GRACE_SEC = 0.3  # 缩短观察窗口提速
        self.popen_mock = mock.MagicMock()
        self.exit_mock = mock.MagicMock()
        self._real_sleep = time.sleep  # 打桩前留存真 sleep，供测试线程使用
        # 默认安装器“存活”（poll 返回 None）；各用例按需覆盖
        self.popen_mock.return_value.poll.return_value = None
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
        app_module._UPDATE_INSTALL_GRACE_SEC = self._old_grace
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_state()

    def _start(self):
        r = self.c.post('/api/app/start_update', json={
            'download_url': self.url, 'version': '1.1.52'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])

    def _wait_status(self, targets, timeout=15):
        deadline = time.time() + timeout
        st = None
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

    def _launched_path(self):
        return self.popen_mock.call_args[0][0][0]


class TestInstallerEarlyExitNoSuicide(_UpdateTestBase):
    """1. 核心加固：安装器早退（非零退出码）→ error，进程绝不自杀"""

    def test_exit_code_1_goes_error_no_exit(self):
        # 复现 v1.1.51 事故现场：安装包启动后立刻以代码 1 退出
        self.popen_mock.return_value.poll.return_value = 1
        self._start()
        st = self._wait_status(('error',))  # 必经 installing → error，只等终态
        self.assertEqual(st, 'error')
        self._wait_done()
        err = app_module._update_state['error']
        self.assertIn('异常退出', err)
        self.assertIn('代码 1', err)
        # 安装器确实被拉起过，但进程绝不自杀
        self.popen_mock.assert_called_once()
        self.exit_mock.assert_not_called()
        # 失败后下载的临时安装包被清理（清理发生在 error 状态可见之后，轮询等落盘）
        path = self._launched_path()
        deadline = time.time() + 5
        while os.path.exists(path) and time.time() < deadline:
            self._real_sleep(0.05)
        self.assertFalse(os.path.exists(path))

    def test_exit_code_2_also_goes_error(self):
        self.popen_mock.return_value.poll.return_value = 2
        self._start()
        st = self._wait_status(('error',))
        self.assertEqual(st, 'error')
        self._wait_done()
        self.assertIn('代码 2', app_module._update_state['error'])
        self.exit_mock.assert_not_called()

    def test_error_state_queryable_by_frontend(self):
        # 前端轮询 update_progress 必须能看到 error + 错误信息（用于回退浏览器下载）
        self.popen_mock.return_value.poll.return_value = 1
        self._start()
        self._wait_status(('error',))
        self._wait_done()
        pr = self.c.get('/api/app/update_progress').get_json()
        self.assertEqual(pr['status'], 'error')
        self.assertIn('异常退出', pr['error'])


class TestInstallerAliveProceeds(_UpdateTestBase):
    """2. 正常路径：安装器存活 / 正常完成 → 维持自杀释放锁逻辑"""

    def test_alive_installer_proceeds_to_exit(self):
        # poll 恒为 None（窗口内一直存活）→ 走完观察窗口后 os._exit(0)
        self._start()
        st = self._wait_status(('installing', 'error'))
        self.assertEqual(st, 'installing', app_module._update_state.get('error'))
        self._wait_done()
        self.exit_mock.assert_called_once_with(0)
        self.popen_mock.assert_called_once()
        args = self.popen_mock.call_args[0][0]
        for flag in ('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'):
            self.assertIn(flag, args)
        try:
            os.remove(args[0])
        except OSError:
            pass

    def test_quick_success_rc0_proceeds_to_exit(self):
        # 安装器正常快速完成（rc==0）→ 提前跳出观察窗口，仍走 os._exit(0)
        self.popen_mock.return_value.poll.return_value = 0
        self._start()
        st = self._wait_status(('installing', 'error'))
        self.assertEqual(st, 'installing', app_module._update_state.get('error'))
        self._wait_done()
        self.exit_mock.assert_called_once_with(0)
        try:
            os.remove(self._launched_path())
        except OSError:
            pass


class TestGraceConfigAndSource(unittest.TestCase):
    """3. 静态检查：加固逻辑真实存在于 app.py"""

    def test_grace_constant_default_8s(self):
        self.assertEqual(app_module._UPDATE_INSTALL_GRACE_SEC, 8)

    def test_source_contains_grace_guard(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn('_UPDATE_INSTALL_GRACE_SEC', src)
        self.assertIn('proc.poll()', src)
        self.assertIn('安装程序异常退出', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
