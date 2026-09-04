# -*- coding: utf-8 -*-
"""v1.1.46 登录恢复与提示区分测试

背景：认证服务器迁移（PythonAnywhere→腾讯云）。此前认证服务器不可用时，
verify_user 一律回退本地数据库并误报"用户名或密码错误"，误导用户。

本次覆盖：
1. verify_user 远程成功（200）→ 正常返回用户
2. 远程明确拒绝（401/403）→ 原样透传业务错误（密码错误/停用）
3. 远程服务不可用（404 / 5xx / 网络503）且本地无此用户 → 明确提示
   "认证服务暂时不可用"，不再误报"用户名或密码错误"
4. 远程服务不可用但本地确有该用户且密码匹配 → 回退本地登录成功
5. login 路由透传 verify_user 真实错误（不再硬编码"用户名或密码错误"）
6. /api/app/check_update 免登录可访问（登录页升级横幅依赖此接口）
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth
from app import app as flask_app

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)


class AuthTestCase(unittest.TestCase):
    """core/auth.verify_user 远程分支行为"""

    @classmethod
    def setUpClass(cls):
        # 强制远程模式（AUTH_SERVER_URL 非空即远程）
        cls._orig_url = auth.AUTH_SERVER_URL
        cls._orig_api_key = auth.AUTH_API_KEY
        auth.AUTH_SERVER_URL = 'http://127.0.0.1:9'
        auth.AUTH_API_KEY = 'test_key'
        cls._orig_remote_request = auth._remote_request
        cls._orig_db_path = auth.DB_PATH

    @classmethod
    def tearDownClass(cls):
        auth.AUTH_SERVER_URL = cls._orig_url
        auth.AUTH_API_KEY = cls._orig_api_key
        auth._remote_request = cls._orig_remote_request
        auth.DB_PATH = cls._orig_db_path

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='ly_auth_test_')
        auth.DB_PATH = os.path.join(self._tmp, 'users.db')
        auth.init_db(self._tmp)

    def tearDown(self):
        auth._remote_request = AuthTestCase._orig_remote_request
        auth.DB_PATH = AuthTestCase._orig_db_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mock_remote(self, result, status):
        auth._remote_request = mock.Mock(return_value=(result, status))

    # ---- 1. 远程成功 ----
    def test_remote_ok(self):
        self._mock_remote(
            {'ok': True, 'user_id': 1, 'username': 'admin',
             'is_admin': True, 'token': 'tk123'}, 200)
        user, err = auth.verify_user('admin', 'pw')
        self.assertIsNone(err)
        self.assertEqual(user['username'], 'admin')
        self.assertTrue(user['is_admin'])
        self.assertEqual(user['_token'], 'tk123')

    # ---- 2. 远程明确拒绝：401 密码错误 / 403 停用 ----
    def test_remote_401_password_wrong(self):
        self._mock_remote({'ok': False, 'error': '用户名或密码错误'}, 401)
        user, err = auth.verify_user('nobody', 'wrong')
        self.assertIsNone(user)
        self.assertIn('密码', err)          # 密码错场景保留原文案

    def test_remote_403_disabled(self):
        self._mock_remote({'ok': False, 'error': '账号已被停用，请联系管理员'}, 403)
        user, err = auth.verify_user('someone', 'pw')
        self.assertIsNone(user)
        self.assertIn('停用', err)

    # ---- 3. 服务不可用（404/5xx/网络）且本地无此用户 → 不再误报密码错误 ----
    def test_remote_404_no_local_user(self):
        # PythonAnywhere 下线时返回的正是 404
        self._mock_remote({'error': '服务器错误 (404)'}, 404)
        user, err = auth.verify_user('admin', 'whatever')
        self.assertIsNone(user)
        self.assertNotIn('密码错误', err)          # 关键：不误导
        self.assertIn('认证服务暂时不可用', err)

    def test_remote_503_network_no_local_user(self):
        self._mock_remote({'error': '无法连接认证服务器，请检查网络'}, 503)
        user, err = auth.verify_user('admin', 'whatever')
        self.assertIsNone(user)
        self.assertIn('认证服务暂时不可用', err)

    def test_remote_500_server_error_no_local_user(self):
        self._mock_remote({'error': '认证服务异常: boom'}, 500)
        user, err = auth.verify_user('admin', 'whatever')
        self.assertIsNone(user)
        self.assertIn('认证服务暂时不可用', err)

    # ---- 4. 服务不可用但本地有该用户（离线注册过）→ 回退本地成功 ----
    def test_remote_down_local_fallback_success(self):
        # 本地预置用户（模拟此前在本地注册过的账号）
        import sqlite3
        conn = sqlite3.connect(auth.DB_PATH)
        conn.execute(
            'INSERT INTO users (username, password_hash, is_admin, created_at) '
            'VALUES (?, ?, 0, ?)',
            ('localuser', auth._hash_password('pw123456'),
             '2026-09-01T00:00:00'))
        conn.commit()
        conn.close()

        self._mock_remote({'error': '无法连接认证服务器'}, 503)
        user, err = auth.verify_user('localuser', 'pw123456')
        self.assertIsNone(err)
        self.assertEqual(user['username'], 'localuser')

        # 密码错误时本地也不放行 → 仍提示服务不可用（不泄露本地用户存在性）
        self._mock_remote({'error': '无法连接认证服务器'}, 503)
        user, err = auth.verify_user('localuser', 'badpassword')
        self.assertIsNone(user)
        self.assertIn('认证服务暂时不可用', err)

    # ---- 5. 本地模式不受影响 ----
    def test_local_mode_wrong_password(self):
        auth.AUTH_SERVER_URL = None
        try:
            user, err = auth.verify_user('ghost', 'x')
            self.assertIsNone(user)
            self.assertEqual(err, '用户名或密码错误')
        finally:
            auth.AUTH_SERVER_URL = 'http://127.0.0.1:9'


class LoginRouteTestCase(unittest.TestCase):
    """app.py login 路由透传 verify_user 真实错误"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()

    @mock.patch('app.verify_user')
    def test_json_login_transparent_error(self, mock_vu):
        # 认证服务不可用 → 前端应看到"认证服务暂时不可用"而非"用户名或密码错误"
        mock_vu.return_value = (None, '认证服务暂时不可用，请检查网络或联系管理员')
        r = self.c.post('/login', json={'username': 'u', 'password': 'p'})
        self.assertEqual(r.status_code, 401)
        data = r.get_json()
        self.assertIn('认证服务暂时不可用', data.get('error', ''))

    @mock.patch('app.verify_user')
    def test_html_login_transparent_error(self, mock_vu):
        # HTML 表单回退提交也应显示真实错误
        mock_vu.return_value = (None, '认证服务暂时不可用，请检查网络或联系管理员')
        r = self.c.post('/login', data={'username': 'u', 'password': 'p'})
        body = r.get_data(as_text=True)
        self.assertIn('认证服务暂时不可用', body)

    def test_check_update_anonymous_accessible(self):
        # 免登录：登录页升级横幅依赖此接口可匿名访问
        r = self.c.get('/api/app/check_update')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('has_update', data)
        self.assertIn('ok', data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
