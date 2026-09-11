# -*- coding: utf-8 -*-
"""v2.5.1 修复测试（授权验收实测发现的两处缺陷）

缺陷1（审计断裂）：oauth.audit() 漏挂落盘 handler，oauth_audit.log 从不生成
    ——方案 5.3 安全规范要求"全链路审计留痕"未落实。
    修复：audit() 首次调用即 _setup_audit_log()。

缺陷2（登录不回跳）：先开授权链接再登录，登录成功后落到门户首页而非授权页
    （login_required 不携带 next，login 成功恒 redirect('/')）。
    修复：login_required 携带 next=完整路径（含查询串）；login 成功回跳
    _safe_next()（仅允许站内相对路径，防开放重定向）；前端 fetch 带查询串、
    优先 data.redirect；原生表单回退 action 动态带 next。
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from core import paths as core_paths
from modules.mcp.core import oauth


class _TmpDirBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ly_v251_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ==================== 1. 审计落盘修复 ====================
class TestAuditFileWritten(_TmpDirBase):
    """oauth_audit.log 必须真实生成并逐条留痕"""

    def setUp(self):
        super().setUp()
        # ⚠️ _setup_audit_log 用的是 oauth 模块命名空间的 data_dir，
        # 必须补丁 oauth.data_dir（补 core_paths 无效）
        self._orig_datadir = oauth.data_dir
        oauth.data_dir = lambda: self.tmp
        self._orig_env = os.environ.get('LY_MCP_NO_ACCESS_FILE')
        os.environ.pop('LY_MCP_NO_ACCESS_FILE', None)
        # 复位一次性挂载标志与既有 handler
        self._orig_done = getattr(oauth._setup_audit_log, '_done', False)
        oauth._setup_audit_log._done = False
        self._orig_handlers = list(oauth.audit_logger.handlers)
        oauth.audit_logger.handlers = []
        oauth.audit_logger.setLevel(logging.INFO)

    def tearDown(self):
        oauth.data_dir = self._orig_datadir
        if self._orig_env is not None:
            os.environ['LY_MCP_NO_ACCESS_FILE'] = self._orig_env
        oauth._setup_audit_log._done = self._orig_done
        oauth.audit_logger.handlers = self._orig_handlers
        super().tearDown()

    def test_audit_creates_file_and_appends(self):
        oauth.audit('consent_granted', client_id='lyapp_x', user='tester')
        oauth.audit('token_issued', client_id='lyapp_x', user='tester')
        path = os.path.join(self.tmp, 'logs', 'oauth_audit.log')
        self.assertTrue(os.path.exists(path), 'audit() 必须落盘生成日志文件')
        with open(path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('consent_granted', content)
        self.assertIn('token_issued', content)
        self.assertIn('lyapp_x', content)
        self.assertEqual(content.count('\n'), 2, '两条审计各占一行')

    def test_audit_setup_called_inside_audit(self):
        """守卫：_setup_audit_log 必须由 audit() 触发（防回归再漏挂）"""
        with mock.patch.object(oauth, '_setup_audit_log',
                               side_effect=oauth._setup_audit_log) as m:
            oauth.audit('app_registered', client_id='lyapp_y')
        m.assert_called_once()


# ==================== 2. 登录回跳修复 ====================
class TestLoginNextRedirect(_TmpDirBase):
    """未登录访问受保护页 → 携带 next 跳登录；登录成功 → 回跳 next"""

    def test_login_required_carries_next(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/apps')
        self.assertEqual(r.status_code, 302)
        loc = r.headers['Location']
        self.assertIn('/login', loc)
        self.assertIn('next=', loc, '跳登录必须携带 next 参数')
        self.assertIn('/mcp/apps', loc, 'next 须包含原路径')

    def test_login_required_keeps_query_string(self):
        with flask_app.test_client() as c:
            r = c.get('/mcp/oauth/authorize',
                      query_string={'client_id': 'lyapp_abc',
                                    'code_challenge': 'xyz',
                                    'code_challenge_method': 'S256'})
        self.assertEqual(r.status_code, 302)
        loc = r.headers['Location']
        self.assertIn('next=', loc)
        # 查询串完整保留（授权参数不丢）
        self.assertIn('code_challenge', loc)

    def test_json_login_returns_redirect_field(self):
        fake_user = {'id': 1, 'username': 'tester', 'is_admin': False,
                     '_token': None}
        with mock.patch('app.verify_user', return_value=(fake_user, None)), \
             mock.patch('app.get_remote_token', return_value=None):
            with flask_app.test_client() as c:
                r = c.post('/login?next=/mcp/oauth/authorize%3Fclient_id%3Dabc',
                           json={'username': 'tester', 'password': 'x'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertIn('/mcp/oauth/authorize', body.get('redirect') or '',
                      'JSON 登录须回传回跳地址')

    def test_safe_next_rejects_open_redirect(self):
        fake_user = {'id': 1, 'username': 'tester', 'is_admin': False,
                     '_token': None}
        for evil in ('//evil.com/x', 'http://evil.com', '\\\\evil.com',
                     'javascript:alert(1)'):
            with mock.patch('app.verify_user',
                            return_value=(fake_user, None)), \
                 mock.patch('app.get_remote_token', return_value=None):
                with flask_app.test_client() as c:
                    r = c.post('/login?next=' + evil.replace(':', '%3A'),
                               json={'username': 'tester', 'password': 'x'})
            self.assertEqual(r.get_json().get('redirect'), '/',
                             '外域 next 必须降级为首页：%s' % evil)

    def test_form_login_redirects_to_next(self):
        fake_user = {'id': 1, 'username': 'tester', 'is_admin': False,
                     '_token': None}
        with mock.patch('app.verify_user', return_value=(fake_user, None)), \
             mock.patch('app.get_remote_token', return_value=None):
            with flask_app.test_client() as c:
                r = c.post('/login?next=/mcp/apps',
                           data={'username': 'tester', 'password': 'x'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/mcp/apps', r.headers['Location'],
                      '原生表单登录成功后必须回跳 next 页面')


# ==================== 3. 前端模板守卫 ====================
class TestLoginTemplate(unittest.TestCase):

    def test_fetch_preserves_query_and_honors_redirect(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'templates', 'login.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn("fetch('/login' + window.location.search", html,
                      'AJAX 登录须携带查询串（含 next）')
        self.assertIn('data.redirect ||', html,
                      '登录成功后须优先使用后端回传的 redirect')
        self.assertIn("formEl.action = '/login' + window.location.search", html,
                      '原生表单回退也须带 next')


if __name__ == '__main__':
    unittest.main()
