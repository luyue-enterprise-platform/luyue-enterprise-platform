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
"""
from flask import (
    Blueprint, Response, jsonify, render_template, request, url_for
)

from core.auth import login_required

from . import __version__ as MCP_VERSION
from .core import protocol, security, tools as tool_registry

mcp_bp = Blueprint(
    'mcp', __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/mcp',
)


def _rpc_url():
    """返回当前实际的 MCP 端点地址（端口可能浮动，故由请求上下文推导）"""
    return request.host_url.rstrip('/') + url_for('mcp.rpc')


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
    """MCP Streamable HTTP 端点：单端点 JSON-RPC 2.0"""
    if not security.is_enabled():
        return jsonify({'error': 'MCP 服务已停用，请在门户页启用'}), 403

    header = request.headers.get('Authorization', '') or ''
    token = header[7:].strip() if header.lower().startswith('bearer ') else ''
    if not security.verify(token):
        return jsonify({'error': '未授权：需要有效的 Bearer Token'}), 401

    raw = request.get_data(as_text=True) or '{}'
    payload, code, notify = protocol.handle_jsonrpc(raw)
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
