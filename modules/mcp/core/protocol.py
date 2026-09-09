# -*- coding: utf-8 -*-
"""MCP 协议层（Streamable HTTP 传输）

以 JSON-RPC 2.0 实现 MCP 服务端所需方法，单端点 POST /mcp：
- initialize            能力协商（协议版本按客户端可支持者回显）
- notifications/*       通知类，按规范不返回响应体
- tools/list            返回工具清单（含 JSON Schema 入参）
- tools/call            执行工具
- ping                  心跳

说明：不引入 mcp SDK，仅用标准库实现，保持零新增依赖，降低 PyInstaller
打包链风险；响应统一 application/json（Streamable HTTP 允许单条 JSON 响应）。
"""
import json

from . import tools as tool_registry

SERVER_NAME = 'luyue-enterprise-platform'
SERVER_VERSION = '2.2.0'
DEFAULT_PROTOCOL_VERSION = '2025-06-18'
SUPPORTED_PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _error(req_id, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': err}


def _is_notification(req):
    return isinstance(req, dict) and req.get('id') is None


def _negotiate_version(client_version):
    if client_version in SUPPORTED_PROTOCOL_VERSIONS:
        return client_version
    return DEFAULT_PROTOCOL_VERSION


def _handle_initialize(req):
    params = req.get('params') or {}
    return _result(req.get('id'), {
        'protocolVersion': _negotiate_version(params.get('protocolVersion')),
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
    })


def _handle_tools_list(req):
    return _result(req.get('id'), {'tools': tool_registry.list_tools()})


def _handle_tools_call(req):
    params = req.get('params') or {}
    name = params.get('name')
    if not name:
        return _error(req.get('id'), INVALID_PARAMS, '缺少工具名称 name')
    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        return _error(req.get('id'), INVALID_PARAMS, 'arguments 必须为对象')
    text, is_error = tool_registry.call_tool(name, arguments)
    return _result(req.get('id'), {
        'content': [{'type': 'text', 'text': text}],
        'isError': bool(is_error),
    })


def _handle_ping(req):
    return _result(req.get('id'), {})


_METHODS = {
    'initialize': _handle_initialize,
    'tools/list': _handle_tools_list,
    'tools/call': _handle_tools_call,
    'ping': _handle_ping,
}


def handle_jsonrpc(raw_body):
    """处理单条 JSON-RPC 请求

    返回 (payload, status_code, notify)
      payload: 响应字典；通知类为 None
      notify:  True 表示该请求是通知（无需响应体）
    """
    try:
        req = json.loads(raw_body)
    except Exception:
        return _error(None, PARSE_ERROR, 'JSON 解析失败'), 400, False

    if isinstance(req, list):
        # 批量请求：逐条处理（MCP 客户端一般不用，做兼容）
        responses = []
        for item in req:
            resp, _code, notify = handle_jsonrpc(json.dumps(item, ensure_ascii=False))
            if not notify and resp is not None:
                responses.append(resp)
        return responses, 200, not responses

    if not isinstance(req, dict):
        return _error(None, INVALID_REQUEST, '请求必须为 JSON 对象'), 400, False

    method = req.get('method')
    if _is_notification(req):
        # notifications/* 按规范不返回响应；但未知通知也静默接受
        return None, 202, True

    if not method:
        return _error(req.get('id'), INVALID_REQUEST, '缺少 method'), 400, False

    handler = _METHODS.get(method)
    if not handler:
        return _error(req.get('id'), METHOD_NOT_FOUND,
                      '不支持的方法: %s' % method), 200, False
    try:
        return handler(req), 200, False
    except Exception as e:
        return _error(req.get('id'), INTERNAL_ERROR, '服务端内部错误: %s' % e), 200, False
