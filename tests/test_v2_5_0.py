# -*- coding: utf-8 -*-
"""v2.5.0 第三方应用授权直连测试（OAuth 2.0 授权码 + PKCE S256）

守卫范围：
1. 应用注册表：登记/删除、secret 哈希落库（不存明文）、roster:read 永不开放
2. 授权请求预检：未知应用/回调白名单/scope 超限/PKCE 强制/S256 限定
3. 授权码换令牌：正确流程、verifier 不符、一次性（重放作废）、令牌哈希落库
4. 刷新轮换：新对签发、旧刷新令牌作废
5. 鉴权与 scope 拦截：authenticate、check_scope、撤销即时生效、过期失效
6. rpc 双通道：静态令牌不受限、OAuth 按 scope 逐工具 403
7. HTTP 端点：/mcp/oauth/token 错误契约、授权页登录态、同意/拒绝路径、撤销端点
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.mcp.core import oauth, security


def _login(c, username='tester', is_admin=False):
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = username
        sess['is_admin'] = is_admin


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


class _OAuthBase(unittest.TestCase):
    """oauth store + security 配置隔离"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ly_v250_')
        self._orig_store = oauth._STORE_PATH
        self._orig_cfg = security._CONFIG_PATH
        oauth._STORE_PATH = os.path.join(self.tmp, 'oauth_store.json')
        security._CONFIG_PATH = os.path.join(self.tmp, 'mcp_config.json')
        oauth._CODES.clear()

    def tearDown(self):
        oauth._STORE_PATH = self._orig_store
        security._CONFIG_PATH = self._orig_cfg
        oauth._CODES.clear()
        _rmtree(self.tmp)

    # ---- 便捷流程 ----
    def _register(self, scopes=None, redirect_uris=None, paste_mode=True,
                  name='测试应用'):
        return oauth.register_app(
            name=name, redirect_uris=redirect_uris,
            scopes=scopes or ['provinces:read', 'tasks:submit'],
            paste_mode=paste_mode, icon='🧪')

    @staticmethod
    def _pkce():
        verifier = 'test_verifier_0123456789abcdefABCDEF'
        challenge = oauth._pkce_s256(verifier)
        return verifier, challenge

    def _full_flow(self, scopes=None):
        """注册 → 授权码 → 换令牌，返回 (client_id, client_secret, tokens)"""
        meta, secret = self._register(scopes=scopes)
        verifier, challenge = self._pkce()
        code = oauth.create_authorization_code(
            meta['client_id'], 'tester', scopes or ['provinces:read'],
            challenge)
        tokens = oauth.exchange_code(code, meta['client_id'], secret, verifier)
        return meta['client_id'], secret, tokens


# ==================== 1. 应用注册表 ====================
class TestAppRegistry(_OAuthBase):

    def test_register_returns_meta_and_secret_once(self):
        meta, secret = self._register()
        self.assertTrue(meta['client_id'].startswith('lyapp_'))
        self.assertTrue(secret.startswith('lysec_'))
        self.assertNotIn('client_secret_hash', meta)

    def test_secret_hashed_not_plaintext(self):
        meta, secret = self._register()
        with open(oauth._STORE_PATH, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn(secret, raw, 'client_secret 明文不得落盘')
        self.assertIn(oauth._sha256_hex(secret), raw)

    def test_roster_scope_never_grantable(self):
        with self.assertRaises(ValueError):
            oauth.register_app(name='越权应用', scopes=['roster:read'])
        self.assertNotIn('roster:read', oauth.GRANTABLE_SCOPES)
        self.assertIn('roster:read', oauth.SCOPES, '定义保留但不可授权')

    def test_register_requires_grantable_scope(self):
        with self.assertRaises(ValueError):
            oauth.register_app(name='空范围', scopes=[])

    def test_delete_app_revokes_all(self):
        client_id, secret, tokens = self._full_flow()
        self.assertIsNotNone(oauth.authenticate(tokens['access_token']))
        oauth.delete_app(client_id)
        self.assertIsNone(oauth.authenticate(tokens['access_token']))
        self.assertEqual(oauth.list_grants(username='tester'), [])

    def test_list_apps_hides_secret_hash(self):
        self._register()
        for app in oauth.list_apps():
            self.assertNotIn('client_secret_hash', app)


# ==================== 2. 授权请求预检 ====================
class TestAuthorizeValidation(_OAuthBase):

    def test_unknown_client_rejected(self):
        app, scopes, err = oauth.validate_authorize_request('lyapp_none')
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 'invalid_request')

    def test_redirect_uri_whitelist_exact_match(self):
        meta, _ = self._register(redirect_uris=['http://127.0.0.1:9999/cb'])
        verifier, challenge = self._pkce()
        _, _, err = oauth.validate_authorize_request(
            meta['client_id'], 'http://127.0.0.1:9999/cb',
            'provinces:read', challenge, 'S256')
        self.assertIsNone(err)
        _, _, err2 = oauth.validate_authorize_request(
            meta['client_id'], 'http://evil.example/cb',
            'provinces:read', challenge, 'S256')
        self.assertIsNotNone(err2, '回调地址必须白名单精确匹配')

    def test_scope_cannot_exceed_registered(self):
        meta, _ = self._register(scopes=['provinces:read'])
        verifier, challenge = self._pkce()
        _, _, err = oauth.validate_authorize_request(
            meta['client_id'], None, 'tasks:submit', challenge, 'S256')
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 'invalid_scope')

    def test_pkce_challenge_mandatory(self):
        meta, _ = self._register()
        _, _, err = oauth.validate_authorize_request(
            meta['client_id'], None, 'provinces:read', None, None)
        self.assertIsNotNone(err, 'PKCE 必须强制')

    def test_plain_challenge_method_rejected(self):
        meta, _ = self._register()
        _, _, err = oauth.validate_authorize_request(
            meta['client_id'], None, 'provinces:read', 'abc', 'plain')
        self.assertIsNotNone(err, '仅支持 S256')


# ==================== 3. 授权码换令牌 ====================
class TestCodeExchange(_OAuthBase):

    def test_full_flow_issues_tokens(self):
        client_id, secret, tokens = self._full_flow()
        self.assertIn('access_token', tokens)
        self.assertIn('refresh_token', tokens)
        self.assertEqual(tokens['token_type'], 'Bearer')
        self.assertIn('provinces:read', tokens['scope'])

    def test_access_token_hashed_in_store(self):
        _, _, tokens = self._full_flow()
        with open(oauth._STORE_PATH, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn(tokens['access_token'], raw, '访问令牌明文不得落盘')
        self.assertNotIn(tokens['refresh_token'], raw, '刷新令牌明文不得落盘')

    def test_wrong_verifier_rejected(self):
        meta, secret = self._register()
        _, challenge = self._pkce()
        code = oauth.create_authorization_code(
            meta['client_id'], 'tester', ['provinces:read'], challenge)
        result = oauth.exchange_code(code, meta['client_id'], secret,
                                     'wrong_verifier_000')
        self.assertEqual(result['error'], 'invalid_grant')

    def test_code_single_use(self):
        client_id, secret, tokens = self._full_flow()
        self.assertIn('access_token', tokens)
        # 重放：再次换（同 code 已消费）→ invalid_grant
        # （code 已在上一步消费，此处只能验证再次交换失败）
        verifier, challenge = self._pkce()
        code2 = oauth.create_authorization_code(
            client_id, 'tester', ['provinces:read'], challenge)
        r1 = oauth.exchange_code(code2, client_id, secret, verifier)
        self.assertIn('access_token', r1)
        r2 = oauth.exchange_code(code2, client_id, secret, verifier)
        self.assertEqual(r2['error'], 'invalid_grant', '授权码必须一次性')

    def test_bad_client_secret_rejected(self):
        meta, _ = self._register()
        verifier, challenge = self._pkce()
        code = oauth.create_authorization_code(
            meta['client_id'], 'tester', ['provinces:read'], challenge)
        result = oauth.exchange_code(code, meta['client_id'], 'bad-secret',
                                     verifier)
        self.assertEqual(result['error'], 'invalid_client')

    def test_exchange_updates_existing_grant(self):
        client_id, secret, _ = self._full_flow()
        grants = oauth.list_grants(username='tester')
        self.assertEqual(len(grants), 1)
        # 同用户同应用再次授权 → 更新而非新增
        verifier, challenge = self._pkce()
        code = oauth.create_authorization_code(
            client_id, 'tester', ['provinces:read'], challenge)
        oauth.exchange_code(code, client_id, secret, verifier)
        self.assertEqual(len(oauth.list_grants(username='tester')), 1)


# ==================== 4. 刷新轮换 ====================
class TestRefreshRotation(_OAuthBase):

    def test_refresh_issues_new_pair_and_invalidates_old(self):
        client_id, secret, tokens = self._full_flow()
        r = oauth.refresh_access_token(tokens['refresh_token'],
                                       client_id, secret)
        self.assertIn('access_token', r)
        self.assertNotEqual(r['access_token'], tokens['access_token'])
        self.assertNotEqual(r['refresh_token'], tokens['refresh_token'])
        # 新访问令牌有效
        self.assertIsNotNone(oauth.authenticate(r['access_token']))
        # 旧刷新令牌已作废（轮换）
        r2 = oauth.refresh_access_token(tokens['refresh_token'],
                                        client_id, secret)
        self.assertEqual(r2['error'], 'invalid_grant')

    def test_refresh_unknown_token_rejected(self):
        client_id, secret, _ = self._full_flow()
        r = oauth.refresh_access_token('lyrt_unknown', client_id, secret)
        self.assertEqual(r['error'], 'invalid_grant')


# ==================== 5. 鉴权与 scope 拦截 ====================
class TestAuthenticateAndScope(_OAuthBase):

    def test_authenticate_returns_scopes(self):
        _, _, tokens = self._full_flow(scopes=['provinces:read'])
        auth = oauth.authenticate(tokens['access_token'])
        self.assertIsNotNone(auth)
        self.assertEqual(auth['scopes'], ['provinces:read'])
        self.assertEqual(auth['username'], 'tester')

    def test_check_scope(self):
        _, _, tokens = self._full_flow(scopes=['provinces:read'])
        auth = oauth.authenticate(tokens['access_token'])
        self.assertEqual(oauth.check_scope(auth, 'insurance_provinces'),
                         'provinces:read')
        self.assertIsNone(oauth.check_scope(auth, 'get_task_status'),
                          'scope 不足必须拦截')
        self.assertIsNone(oauth.check_scope(auth, 'not_a_tool'))

    def test_revoke_takes_effect_immediately(self):
        client_id, secret, tokens = self._full_flow()
        self.assertIsNotNone(oauth.authenticate(tokens['access_token']))
        grants = oauth.list_grants(username='tester')
        self.assertTrue(oauth.revoke_grant(grants[0]['id'],
                                           username='tester'))
        self.assertIsNone(oauth.authenticate(tokens['access_token']),
                          '撤销必须即时生效')

    def test_expired_access_token_rejected(self):
        client_id, secret, tokens = self._full_flow()
        # 直接改库把过期时间改到过去
        with oauth._LOCK:
            store = oauth._load()
            for t in store['tokens']:
                t['access_expires_at'] = 1
            oauth._save(store)
        self.assertIsNone(oauth.authenticate(tokens['access_token']))

    def test_tool_scope_mapping_complete(self):
        for tool in ['insurance_provinces', 'insurance_calculate',
                     'convert_pdf_to_word', 'convert_to_pdf',
                     'contract_organize', 'get_task_status',
                     'get_task_result']:
            self.assertIn(tool, oauth.TOOL_SCOPES)
        self.assertEqual(oauth.required_scope('get_task_result'),
                         'results:read')


# ==================== 6. rpc 双通道 ====================
class TestRpcDualChannel(_OAuthBase):

    def setUp(self):
        super().setUp()
        self.static_token = security.get_token()

    def _rpc(self, payload, token):
        with flask_app.test_client() as c:
            return c.post('/mcp/rpc', json=payload,
                          headers={'Authorization': 'Bearer %s' % token})

    def test_static_token_still_full_access(self):
        r = self._rpc({'jsonrpc': '2.0', 'id': 1,
                       'method': 'tools/call',
                       'params': {'name': 'insurance_provinces',
                                  'arguments': {}}}, self.static_token)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['result']['isError'],
                         '静态令牌（兼容模式）不受 scope 限制')

    def test_oauth_token_sufficient_scope_passes(self):
        _, _, tokens = self._full_flow(scopes=['provinces:read'])
        r = self._rpc({'jsonrpc': '2.0', 'id': 1,
                       'method': 'tools/call',
                       'params': {'name': 'insurance_provinces',
                                  'arguments': {}}}, tokens['access_token'])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['result']['isError'])

    def test_oauth_token_insufficient_scope_403(self):
        _, _, tokens = self._full_flow(scopes=['provinces:read'])
        r = self._rpc({'jsonrpc': '2.0', 'id': 1,
                       'method': 'tools/call',
                       'params': {'name': 'get_task_status',
                                  'arguments': {'task_id': 'x'}}},
                      tokens['access_token'])
        self.assertEqual(r.status_code, 403)
        body = r.get_json()
        self.assertEqual(body['error'], 'insufficient_scope')
        self.assertEqual(body['required_scope'], 'tasks:read')

    def test_oauth_token_non_tool_call_not_gated(self):
        _, _, tokens = self._full_flow(scopes=['provinces:read'])
        r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                      tokens['access_token'])
        self.assertEqual(r.status_code, 200)

    def test_revoked_oauth_token_401(self):
        client_id, secret, tokens = self._full_flow()
        grants = oauth.list_grants(username='tester')
        oauth.revoke_grant(grants[0]['id'], username='tester')
        r = self._rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                      tokens['access_token'])
        self.assertEqual(r.status_code, 401)


# ==================== 7. HTTP 端点 ====================
class TestOAuthEndpoints(_OAuthBase):

    def _register_http(self, scopes=None, redirect_uris=None,
                       paste_mode=True):
        meta, secret = self._register(scopes=scopes,
                                      redirect_uris=redirect_uris,
                                      paste_mode=paste_mode)
        return meta, secret

    def test_authorize_page_requires_login(self):
        meta, _ = self._register_http()
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            r = c.get('/mcp/oauth/authorize', query_string={
                'client_id': meta['client_id'],
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
            })
        self.assertEqual(r.status_code, 302, '未登录须跳登录页')

    def test_authorize_page_shows_consent(self):
        meta, _ = self._register_http()
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            _login(c)
            r = c.get('/mcp/oauth/authorize', query_string={
                'client_id': meta['client_id'],
                'scope': 'provinces:read',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
            })
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('测试应用', body)
        self.assertIn('同 意 授 权', body)
        self.assertIn('拒 绝', body, '拒绝按钮必须与同意同级可见')

    def test_authorize_invalid_request_no_page(self):
        with flask_app.test_client() as c:
            _login(c)
            r = c.get('/mcp/oauth/authorize', query_string={
                'client_id': 'lyapp_unknown',
                'code_challenge': 'x', 'code_challenge_method': 'S256',
            })
        self.assertEqual(r.status_code, 400, '预检不通过拒绝展示确认页')

    def test_consent_allow_paste_mode_shows_code(self):
        meta, _ = self._register_http()
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            _login(c)
            r = c.post('/mcp/oauth/authorize', data={
                'client_id': meta['client_id'],
                'scope': 'provinces:read',
                'state': '',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
                'decision': 'allow',
            })
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('授权成功', body)

    def test_consent_deny_no_credentials(self):
        meta, _ = self._register_http()
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            _login(c)
            r = c.post('/mcp/oauth/authorize', data={
                'client_id': meta['client_id'],
                'scope': 'provinces:read',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
                'decision': 'deny',
            })
        self.assertEqual(r.status_code, 200)
        self.assertIn('拒绝', r.get_data(as_text=True))
        self.assertEqual(oauth.list_grants(username='tester'), [],
                         '拒绝不得留任何授权记录')

    def test_consent_deny_with_redirect(self):
        meta, _ = self._register_http(redirect_uris=['http://127.0.0.1:9/cb'],
                                      paste_mode=False)
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            _login(c)
            r = c.post('/mcp/oauth/authorize', data={
                'client_id': meta['client_id'],
                'redirect_uri': 'http://127.0.0.1:9/cb',
                'scope': 'provinces:read',
                'state': 'xyz',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
                'decision': 'deny',
            })
        self.assertEqual(r.status_code, 302)
        self.assertIn('error=access_denied', r.headers['Location'])
        self.assertIn('state=xyz', r.headers['Location'])

    def test_token_endpoint_exchange(self):
        meta, secret = self._register_http()
        verifier, challenge = self._pkce()
        with flask_app.test_client() as c:
            _login(c)
            r = c.post('/mcp/oauth/authorize', data={
                'client_id': meta['client_id'],
                'scope': 'provinces:read',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
                'decision': 'allow',
            })
            # 粘贴模式：从页面提取授权码
            body = r.get_data(as_text=True)
            start = body.find('lycode_')
            code = body[start:start + 200].split('<')[0].split()[0]
            r2 = c.post('/mcp/oauth/token', data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': meta['client_id'],
                'client_secret': secret,
                'code_verifier': verifier,
            })
        self.assertEqual(r2.status_code, 200)
        tokens = r2.get_json()
        self.assertIn('access_token', tokens)

    def test_token_endpoint_unsupported_grant(self):
        with flask_app.test_client() as c:
            r = c.post('/mcp/oauth/token', data={'grant_type': 'password'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['error'], 'unsupported_grant_type')

    def test_grants_revoke_endpoint_owner_only(self):
        client_id, secret, tokens = self._full_flow()
        grants = oauth.list_grants(username='tester')
        gid = grants[0]['id']
        with flask_app.test_client() as c:
            _login(c, username='other_user')
            r = c.post('/mcp/api/grants/revoke',
                       json={'grant_id': gid})
            self.assertEqual(r.status_code, 404, '非管理员不得撤他人授权')
        with flask_app.test_client() as c:
            _login(c, username='tester')
            r = c.post('/mcp/api/grants/revoke', json={'grant_id': gid})
            self.assertEqual(r.status_code, 200)
        self.assertIsNone(oauth.authenticate(tokens['access_token']))

    def test_apps_register_requires_admin(self):
        with flask_app.test_client() as c:
            _login(c, username='tester', is_admin=False)
            r = c.post('/mcp/api/apps/register',
                       json={'name': 'x', 'scopes': ['provinces:read']})
            self.assertEqual(r.status_code, 403)
        with flask_app.test_client() as c:
            _login(c, username='boss', is_admin=True)
            r = c.post('/mcp/api/apps/register',
                       json={'name': '管理员登记', 'scopes': ['provinces:read']})
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertIn('client_secret', body)


# ==================== 8. 模板守卫 ====================
class TestTemplates(_OAuthBase):

    def test_consent_template_marks_sensitive_scope(self):
        path = os.path.join(os.path.dirname(oauth.__file__), '..',
                            'templates', 'oauth_authorize.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('{% if s.sensitive %}sensitive{% endif %}', html,
                      '高敏感权限须醒目标注')
        self.assertIn('涉及结果数据', html, '敏感标注文案')
        self.assertIn('数据访问范围', html, '权限卡片须展示数据访问范围')
        # 同意与拒绝均为 submit 按钮（同级可见）
        self.assertIn('value="allow"', html)
        self.assertIn('value="deny"', html)

    def test_apps_template_lists_roster_as_never_open(self):
        path = os.path.join(os.path.dirname(oauth.__file__), '..',
                            'templates', 'mcp_apps.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('永不开放', html)
        self.assertIn('系统默认应用', html, '静态令牌单列为兼容模式')
        self.assertIn('disabled', html, 'roster:read 勾选框必须禁用')


if __name__ == '__main__':
    unittest.main()
