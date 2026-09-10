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
"""
import json
import logging
import os
import time

from flask import (
    Blueprint, Response, jsonify, render_template, request, url_for
)

from core.auth import login_required

from . import __version__ as MCP_VERSION
from .core import protocol, security, tools as tool_registry

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
    """MCP Streamable HTTP 端点：单端点 JSON-RPC 2.0"""
    if not security.is_enabled():
        return jsonify({'error': 'MCP 服务已停用，请在门户页启用'}), 403

    header = request.headers.get('Authorization', '') or ''
    token = header[7:].strip() if header.lower().startswith('bearer ') else ''
    if not security.verify(token):
        return jsonify({'error': '未授权：需要有效的 Bearer Token'}), 401

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
