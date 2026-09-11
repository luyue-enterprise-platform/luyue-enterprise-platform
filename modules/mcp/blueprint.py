# -*- coding: utf-8 -*-
"""MCP 服务端 — Flask Blueprint

路由：
- GET  /mcp/            门户页（需登录）：端点地址、令牌、工具清单、总开关
- POST /mcp/rpc         MCP 端点（Bearer Token 鉴权，不依赖浏览器会话）
- GET  /mcp/api/status  服务状态
- POST /mcp/api/token/regenerate   重新生成令牌
- POST /mcp/api/toggle  启用/停用

鉴权分工：门户页与状态管理走平台会话（core.auth.login_required）；
MCP 端点走 Bearer Token——AI 客户端无法完成浏览器登录，令牌由用户在门户页获取。

性能排查（v2.3.4）：/mcp/rpc 每次调用记一条访问日志（方法/工具/任务/耗时/结果），
落盘 <数据目录>/logs/mcp_access.log（滚动 5MB×3）并随根日志输出到控制台；
用于区分「平台任务慢」与「AI 轮询回合慢」——access 日志里同 task 的调用间隔
即 AI 侧节奏，elapsed_ms 即平台侧单次处理耗时。

第三方应用授权直连（v2.5.0）：
- GET  /mcp/oauth/authorize   授权确认页（登录态；校验 client/回调/scope/PKCE）
- POST /mcp/oauth/authorize   同意/拒绝 → 签发授权码（回调 302 或粘贴模式展示）
- POST /mcp/oauth/token       授权码换令牌 / 刷新令牌轮换（OAuth 2.0 + PKCE S256）
- GET  /mcp/apps              应用授权管理页（登录态；授权列表+撤销+管理员登记）
- POST /mcp/api/apps/register / delete        管理员登记/删除第三方应用
- POST /mcp/api/grants/revoke 撤销授权（用户撤自己的，管理员可撤任意）

双通道：静态令牌 =「系统默认应用（兼容模式）」全 scope（WorkBuddy 连接器等）；
OAuth 令牌按授权 scope 受限。rpc() 先静态后 OAuth，scope 逐次拦截。
"""
import json
import logging
import os
import time

from flask import (
    Blueprint, Response, jsonify, redirect, render_template, request, session,
    url_for
)

from core.auth import admin_required, login_required

from . import __version__ as MCP_VERSION
from .core import oauth, protocol, security, tools as tool_registry

access_logger = logging.getLogger('mcp.access')

mcp_bp = Blueprint(
    'mcp', __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/mcp',
)


def _rpc_url():
    """返回当前实际的 MCP 端点地址（端口可能浮动，故由请求上下文推导）"""
    return request.host_url.rstrip('/') + url_for('mcp.rpc')


def _setup_access_log():
    """访问日志落盘 handler（滚动 5MB×3）；目录不可写等异常静默降级为仅控制台，
    绝不影响请求本身"""
    if getattr(_setup_access_log, '_done', False):
        return
    _setup_access_log._done = True
    if os.environ.get('LY_MCP_NO_ACCESS_FILE') == '1':
        return  # 测试隔离：只走控制台/捕获，不写实盘
    try:
        from logging.handlers import RotatingFileHandler
        from core.paths import data_dir
        log_dir = os.path.join(data_dir(), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, 'mcp_access.log'),
            maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        access_logger.addHandler(handler)
    except Exception:
        pass


def _log_access(raw, payload, code, notify, started):
    """MCP 访问日志（v2.3.4）：方法/工具/任务ID/结果码/单次耗时

    排查'AI 反馈很慢'时的定界依据：同 task 相邻调用的时间差 = AI 回合节奏；
    本日志的耗时 = 平台侧该次调用的真实处理耗时。任何异常都不得影响响应。
    """
    try:
        _setup_access_log()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        method = tool = task_id = None
        is_error = ''
        try:
            req = json.loads(raw)
            if isinstance(req, dict):
                method = req.get('method')
                params = req.get('params') or {}
                if isinstance(params, dict):
                    tool = params.get('name')
                    args = params.get('arguments') or {}
                    if isinstance(args, dict):
                        task_id = (args.get('task_id') or
                                   args.get('confirm_task_id'))
            if isinstance(payload, dict):
                result = payload.get('result') or {}
                if isinstance(result, dict) and result.get('isError'):
                    is_error = ' isError'
                if payload.get('error'):
                    is_error = ' rpcError'
        except Exception:
            pass
        access_logger.info(
            '[mcp] %s method=%s tool=%s task=%s code=%s%s %dms',
            request.remote_addr, method or '-', tool or '-', task_id or '-',
            202 if notify else code, is_error, elapsed_ms)
    except Exception:
        pass


@mcp_bp.route('/')
@login_required
def index():
    return render_template('mcp.html',
                           version=MCP_VERSION,
                           endpoint=_rpc_url(),
                           enabled=security.is_enabled(),
                           token=security.get_token(),
                           tools=tool_registry.list_tools())


@mcp_bp.route('/rpc', methods=['POST'])
def rpc():
    """MCP Streamable HTTP 端点：单端点 JSON-RPC 2.0

    双通道鉴权（v2.5.0）：静态令牌（系统默认应用·兼容模式，全 scope）优先；
    未命中再验 OAuth 访问令牌（按授权 scope 逐次拦截 tools/call）。
    """
    if not security.is_enabled():
        return jsonify({'error': 'MCP 服务已停用，请在门户页启用'}), 403

    header = request.headers.get('Authorization', '') or ''
    token = header[7:].strip() if header.lower().startswith('bearer ') else ''
    oauth_auth = None
    if not security.verify(token):
        oauth_auth = oauth.authenticate(token)
        if oauth_auth is None:
            return jsonify({'error': '未授权：需要有效的 Bearer Token'}), 401

    # scope 拦截（仅 OAuth 通道；静态令牌为系统默认应用，不受限）
    if oauth_auth is not None:
        try:
            req = json.loads(request.get_data(as_text=True) or '{}')
        except Exception:
            req = {}
        if isinstance(req, dict) and req.get('method') == 'tools/call':
            tool_name = (req.get('params') or {}).get('name')
            if oauth.required_scope(tool_name) is None:
                oauth.audit('scope_denied', client_id=oauth_auth['client_id'],
                            tool=tool_name, reason='not_open_to_oauth')
                return jsonify({
                    'error': '该工具不开放给第三方应用授权通道',
                    'required_scope': None,
                }), 403
            if oauth.check_scope(oauth_auth, tool_name) is None:
                oauth.audit('scope_denied', client_id=oauth_auth['client_id'],
                            tool=tool_name,
                            required=oauth.required_scope(tool_name))
                return jsonify({
                    'error': 'insufficient_scope',
                    'error_description': '授权范围不足，请在应用授权管理中扩权后重新授权',
                    'required_scope': oauth.required_scope(tool_name),
                }), 403
        oauth.audit('call', client_id=oauth_auth['client_id'],
                    user=oauth_auth.get('username'))

    raw = request.get_data(as_text=True) or '{}'
    started = time.monotonic()
    payload, code, notify = protocol.handle_jsonrpc(raw)
    _log_access(raw, payload, code, notify, started)
    if notify or payload is None:
        # notifications/* 按规范不返回响应体
        return Response(status=202)
    return jsonify(payload), code


@mcp_bp.route('/rpc', methods=['GET'])
def rpc_get():
    """不提供 SSE 流，按规范返回 405 并说明"""
    return jsonify({
        'error': '本服务仅支持 POST JSON-RPC（不提供 SSE 流）',
        'endpoint': _rpc_url(),
    }), 405


@mcp_bp.route('/api/status')
@login_required
def api_status():
    return jsonify({
        'enabled': security.is_enabled(),
        'endpoint': _rpc_url(),
        'token': security.get_token(),
        'version': MCP_VERSION,
        'tools': [{'name': t['name'], 'description': t['description']}
                  for t in tool_registry.list_tools()],
    })


@mcp_bp.route('/api/token/regenerate', methods=['POST'])
@login_required
def api_regenerate_token():
    return jsonify({'token': security.regenerate_token()})


@mcp_bp.route('/api/toggle', methods=['POST'])
@login_required
def api_toggle():
    data = request.get_json(silent=True) or {}
    value = data.get('enabled')
    if value is None:
        value = request.form.get('enabled')
    enabled = security.set_enabled(str(value).lower() in ('1', 'true', 'yes', 'on'))
    return jsonify({'enabled': enabled})


@mcp_bp.route('/api/health')
def api_health():
    """免登录健康检查（仅返回是否启用，不泄露令牌）"""
    return jsonify({'ok': True, 'enabled': security.is_enabled()})


# ---------------------------------------------------------------------------
# 第三方应用授权直连（v2.5.0）— OAuth 2.0 授权码 + PKCE(S256)
# ---------------------------------------------------------------------------

def _authorize_params():
    """从 query/form 提取授权请求参数"""
    src = request.values
    return {
        'client_id': (src.get('client_id') or '').strip(),
        'redirect_uri': (src.get('redirect_uri') or '').strip(),
        'scope': (src.get('scope') or '').strip(),
        'state': src.get('state') or '',
        'code_challenge': (src.get('code_challenge') or '').strip(),
        'code_challenge_method': (src.get('code_challenge_method') or 'S256').strip(),
    }


@mcp_bp.route('/oauth/authorize', methods=['GET', 'POST'])
@login_required
def oauth_authorize():
    """授权确认页：GET 展示（预检不通过拒绝展示），POST 处理同意/拒绝"""
    params = _authorize_params()
    app, scopes, error = oauth.validate_authorize_request(
        params['client_id'], params['redirect_uri'], params['scope'],
        params['code_challenge'], params['code_challenge_method'])
    if error:
        # 不泄露内部细节、不提供重试按钮（防探测）
        oauth.audit('authorize_rejected', client_id=params['client_id'],
                    reason=error[0])
        return render_template('oauth_result.html', kind='invalid',
                               message='授权请求无效：' + error[1]), 400

    if request.method == 'GET':
        scope_cards = [dict(oauth.SCOPES[s], scope=s) for s in scopes]
        return render_template('oauth_authorize.html', app=app,
                               scopes=scope_cards, params=params)

    # POST：用户决定
    username = session.get('username') or ''
    decision = (request.form.get('decision') or '').strip().lower()
    if decision != 'allow':
        oauth.record_denial(params['client_id'], username)
        if params['redirect_uri']:
            return redirect(_deny_redirect(params['redirect_uri'], params['state']))
        return render_template('oauth_result.html', kind='denied')

    code = oauth.create_authorization_code(
        params['client_id'], username, scopes, params['code_challenge'])
    if params['redirect_uri']:
        # 回调模式：302 跳回应用（code + state）
        sep = '&' if '?' in params['redirect_uri'] else '?'
        url = '%s%scode=%s&state=%s' % (params['redirect_uri'], sep,
                                         code, params['state'])
        return redirect(url)
    # 粘贴模式：页面展示授权码（一键复制 + 倒计时）
    return render_template('oauth_result.html', kind='code', code=code,
                           ttl_minutes=oauth.CODE_TTL_SECONDS // 60)


def _deny_redirect(redirect_uri, state):
    sep = '&' if '?' in redirect_uri else '?'
    from urllib.parse import quote
    return '%s%serror=access_denied&state=%s' % (redirect_uri, sep, quote(state or ''))


@mcp_bp.route('/oauth/token', methods=['POST'])
def oauth_token():
    """令牌端点（无登录态）：授权码换令牌 / 刷新令牌轮换；标准 OAuth 错误契约"""
    grant_type = (request.form.get('grant_type') or '').strip()
    client_id = (request.form.get('client_id') or '').strip()
    client_secret = request.form.get('client_secret') or ''
    if grant_type == 'authorization_code':
        result = oauth.exchange_code(
            request.form.get('code') or '', client_id, client_secret,
            request.form.get('code_verifier') or '')
    elif grant_type == 'refresh_token':
        result = oauth.refresh_access_token(
            request.form.get('refresh_token') or '', client_id, client_secret)
    else:
        result = {'error': 'unsupported_grant_type',
                  'error_description': '仅支持 authorization_code / refresh_token'}
    if 'error' in result:
        status = 401 if result['error'] == 'invalid_client' else 400
        return jsonify(result), status
    return jsonify(result)


@mcp_bp.route('/apps')
@login_required
def apps_page():
    """应用授权管理页：静态令牌单列为系统默认应用（兼容模式）"""
    username = session.get('username') or ''
    is_admin = bool(session.get('is_admin'))
    grants = oauth.list_grants(username=None if is_admin else username)
    return render_template(
        'mcp_apps.html', grants=grants,
        static_token=security.get_token(),
        static_enabled=security.is_enabled(),
        apps=oauth.list_apps() if is_admin else [],
        scopes=oauth.SCOPES, is_admin=is_admin)


@mcp_bp.route('/api/apps/register', methods=['POST'])
@admin_required
def api_apps_register():
    """管理员登记第三方应用；client_secret 明文仅本次返回"""
    data = request.get_json(silent=True) or request.form or {}
    try:
        meta, secret = oauth.register_app(
            name=data.get('name'),
            redirect_uris=[u.strip() for u in (data.get('redirect_uris') or [])
                           if str(u).strip()],
            scopes=data.get('scopes') or [],
            paste_mode=str(data.get('paste_mode', 'true')).lower()
            in ('1', 'true', 'yes', 'on'),
            icon=data.get('icon') or '🔌')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    meta['client_secret'] = secret
    return jsonify(meta)


@mcp_bp.route('/api/apps/delete', methods=['POST'])
@admin_required
def api_apps_delete():
    data = request.get_json(silent=True) or request.form or {}
    client_id = (data.get('client_id') or '').strip()
    if not client_id:
        return jsonify({'error': '缺少 client_id'}), 400
    oauth.delete_app(client_id)
    return jsonify({'ok': True})


@mcp_bp.route('/api/grants/revoke', methods=['POST'])
@login_required
def api_grants_revoke():
    """撤销授权：用户撤自己的；管理员可撤任意（令牌即刻失效）"""
    data = request.get_json(silent=True) or request.form or {}
    grant_id = (data.get('grant_id') or '').strip()
    if not grant_id:
        return jsonify({'error': '缺少 grant_id'}), 400
    username = session.get('username') or ''
    as_admin = bool(session.get('is_admin'))
    if not oauth.revoke_grant(grant_id, username=username, as_admin=as_admin):
        return jsonify({'error': '授权不存在或无权撤销'}), 404
    return jsonify({'ok': True})
