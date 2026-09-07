# -*- coding: utf-8 -*-
"""v1.1.56 自动升级修复测试（最终方案：cmd 延时启动器 + 去 AppMutex + 单实例守护）

问题：自动更新下载完成后仍需手动退出软件、再双击安装程序才能升级。
根因链（逐层实测复现）：
  A. Windows 运行中的 EXE 映像被系统锁定、不可覆盖（v1.1.55 的直接根因）。
  B. AppMutex=<name>：升级方自身持有互斥量 → 静默安装器启动即判“正在运行”
     → 默认 Cancel → rc=1。
  C. Inno 6.x 默认开启 CloseApplications，用 Restart Manager 在安装起始关进程；
     本机 RM 关不掉运行中应用 → 静默 Abort → rc=5。
  D. 即便 CloseApplications=no，Inno 流式解压、主 exe 是 [Files] 首项，安装器
     启动 ~0.1s 即尝试 DeleteFile 仍被锁定的主 exe，撞锁重试 ~5s → rc=5。
最终修复：
  1. installer.iss：无 AppMutex（B）+ 显式 CloseApplications=no（C）；
  2. app.py _do_update：改经 `cmd /c ping -n N & start` 延时 ~10s 才真正拉起安装器
     （D）——本进程 2.5s 短观察后立即 os._exit(0) 释放 EXE 锁，安装器真正写主 exe
     时锁早已释放 → 覆盖成功 → [Run] postinstall 自动重启，全程无人值守；
  3. launcher.py 互斥量改为真正的单实例守护（ERROR_ALREADY_EXISTS 即退出），
     防双实例同时锁定主 EXE。
"""
import functools
import http.server
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
import app as app_module

# 复用 v1.1.52 测试的公共脚手架语义（本地 HTTP 假安装包 + 全打桩）
from tests.test_v1_1_52 import _QuietHandler, _UpdateTestBase  # noqa: E402


class TestQuickExitReleasesLock(_UpdateTestBase):
    """1. 核心修复：安装器存活 → 主进程尽快退出释放 EXE 锁（不再 sleep 5）"""

    def test_alive_installer_exits_promptly(self):
        # poll 恒为 None → 短观察窗口走完 → 立即 os._exit(0)，不再长时间拖锁
        self._start()
        st = self._wait_status(('installing', 'error'))
        self.assertEqual(st, 'installing', app_module._update_state.get('error'))
        self._wait_done()
        self.exit_mock.assert_called_once_with(0)

    def test_poll_interval_short(self):
        # 轮询步进为 0.2s（非 0.5s），窗口内判定更及时，尽快进入释放锁阶段
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn('time.sleep(0.2)', src)

    def test_no_long_sleep_before_exit(self):
        # 旧代码 os._exit 前有 time.sleep(5)——锁释放被拖后 5 秒，已删除
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
        # 提取 _do_update 函数体，确认 sleep(5) 不存在
        start = src.index('def _do_update')
        end = src.index('@app.route', start)
        body = src[start:end]
        self.assertNotIn('time.sleep(5)', body)
        self.assertNotIn('sleep(5)', body)


class TestInstallerRejectSemanticsPreserved(_UpdateTestBase):
    """2. v1.1.52 防坏包语义不回退：早退非零仍 error 且不自杀"""

    def test_bad_installer_early_exit_still_protected(self):
        self.popen_mock.return_value.poll.return_value = 1
        self._start()
        st = self._wait_status(('error',))
        self.assertEqual(st, 'error')
        self._wait_done()
        self.assertIn('异常退出', app_module._update_state['error'])
        self.exit_mock.assert_not_called()


class TestCmdDelayedLaunch(unittest.TestCase):
    """2.5 v1.1.56 最终方案：cmd /c + ping 延时启动器（绕开流式解压撞锁 rc=5）"""

    def setUp(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, 'app.py'), 'r', encoding='utf-8') as f:
            src = f.read()
        self.start = src.index('def _do_update')
        self.end = src.index('@app.route', self.start)
        self.body = src[self.start:self.end]

    def test_launch_delay_constant_default(self):
        # 延时必须足够长：> 观察窗口(2.5s) + 本进程退出耗时，确保安装器真正写
        # 主 exe 时锁已释放
        self.assertGreaterEqual(app_module._UPDATE_LAUNCH_DELAY_SEC, 8)

    def test_source_uses_cmd_delayed_launch(self):
        self.assertIn('subprocess.Popen(', self.body)
        self.assertIn('cmd_exe', self.body)
        # ping 延时公式：ping -n N 实耗约 N-1 秒，故 +1 凑足延时
        self.assertIn('ping -n {delay_pings}', self.body)
        self.assertIn('{delay_pings}', self.body)
        self.assertIn('CREATE_NO_WINDOW', self.body)
        self.assertIn('> nul &', self.body)

    def test_no_direct_popen_of_installer(self):
        # 回归防线：不得再直接 Popen 安装器 exe（无延时 → 0.1s 即撞锁 rc=5）
        self.assertNotIn('Popen([\n            tmp_path', self.body)


class TestInstallerIssInert(unittest.TestCase):
    """3. 静态检查：installer.iss 惰性——无 AppMutex、显式 CloseApplications=no"""

    def setUp(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, 'installer.iss'), 'r', encoding='utf-8') as f:
            self.iss = f.read()

    def test_app_mutex_absent(self):
        # 核心修订①：AppMutex 会让升级实例(持有互斥量)启动的静默安装器立即
        # 取消(rc=1)。注释里允许出现 "AppMutex=" 字样（解释为何不能用），配置行不允许。
        self.assertIsNone(re.search(r'^AppMutex=', self.iss, re.M))

    def test_close_applications_disabled(self):
        # 核心修订②：Inno 6.x 默认开启 CloseApplications（Restart Manager 在安装
        # 起始阶段即尝试关闭占用文件的运行中进程，关不掉→静默 Abort rc=5），必须
        # 显式 =no；安装器命令串也不得带 /CLOSEAPPLICATIONS（注释区在 Popen 之前）。
        self.assertIsNotNone(re.search(r'^CloseApplications=no', self.iss, re.M))
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), 'app.py'), 'r', encoding='utf-8') as f:
            app_src = f.read()
        start = app_src.index('def _do_update')
        end = app_src.index('@app.route', start)
        body = app_src[start:end]
        # v1.1.56：Popen 已改为 cmd /c 整串命令，安装器参数区即 Popen 之后的文本
        popen_start = body.index('subprocess.Popen(')
        popen_tail = body[popen_start:end]
        self.assertIn('/VERYSILENT', popen_tail)
        self.assertIn('/NORESTART', popen_tail)
        self.assertNotIn('/CLOSEAPPLICATIONS', popen_tail)

    def test_comment_explains_no_mutex(self):
        # 注释必须说明为何不能配（防后人重新加回去）
        self.assertIn('绝不能配', self.iss)
        self.assertIn('rc=1', self.iss)
        self.assertIn('rc=5', self.iss)

    def test_comment_explains_upgrade(self):
        self.assertIn('v1.1.56 自动升级', self.iss)


class TestLauncherSingletonGuardAndCleanup(unittest.TestCase):
    """4. 静态检查：launcher.py 单实例守护 + 清理升级残留"""

    def setUp(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, 'launcher.py'), 'r', encoding='utf-8') as f:
            self.src = f.read()

    def test_singleton_guard(self):
        # 互斥量必须带 ERROR_ALREADY_EXISTS(=183) 判定：第二个实例直接退出
        self.assertIn("CreateMutexW(None, False, 'LuyuePlatform_SingletonMutex')", self.src)
        self.assertIn('GetLastError()', self.src)
        self.assertIn('183', self.src)
        self.assertIn('已在运行', self.src)

    def test_stale_update_cleanup(self):
        self.assertIn("glob.glob(os.path.join(tempfile.gettempdir(), 'ly_update_*.exe'))", self.src)
        self.assertIn('os.path.getmtime', self.src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
