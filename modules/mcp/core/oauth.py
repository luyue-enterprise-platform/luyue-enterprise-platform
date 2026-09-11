# -*- coding: utf-8 -*-
"""第三方应用授权直连（v2.5.0）— OAuth 2.0 授权码模式 + PKCE(S256)

参照《第三方应用授权直连方案.md》实现，标准库自研，不引入 OAuth 框架。

职责边界：
- 应用注册表（管理员登记）：client_id/client_secret（哈希落库，明文仅创建时展示一次）、
  回调地址白名单（精确匹配）、授权码粘贴模式（桌面/CLI 应用无回调）、可申请 scope 上限
- 授权码生命周期：10 分钟有效、一次性、重放即作废该应用全部令牌（防重放）
- 令牌生命周期：访问令牌 30 天 / 刷新令牌 90 天，轮换使旧刷新令牌作废；
  访问/刷新令牌均**哈希落库**（平台不存明文）
- scope 拦截：每个 MCP 工具映射到唯一所需 scope；鉴权层逐次校验
- 审计：发起/同意/拒绝/换令牌/刷新/调用/撤销逐条留痕（oauth.audit 日志体系，
  落盘 <数据目录>/logs/oauth_audit.log，滚动 5MB×3）

与静态令牌通道的关系（双通道共存）：
- 静态令牌（security.py）=「系统默认应用（兼容模式）」，全 scope，供 WorkBuddy
  连接器等只认静态令牌的客户端使用，行为完全不变
- OAuth 令牌 = 按授权 scope 受限的第三方通道；两者由 blueprint.rpc 统一分流

持久化：<数据目录>/data/oauth_store.json（apps/grants/tokens，均只存哈希与元数据）。
授权码属短命数据，仅存内存（平台重启即失效，符合 10 分钟时效设计）。

合规（个保法最小必要）：scope 按「能力+数据敏感度」分层；roster:read 在清单中
**定义但不开放**——不进入可授权集合，任何应用都无法通过注册拿到。
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time

from core.paths import data_dir

_LOCK = threading.RLock()
_STORE_PATH = None

# ---------------- scope 定义（v2.5.0 权限清单） ----------------

SCOPES = {
    'provinces:read': {
        'title': '查询支持的省份列表',
        'usage': '读取平台当前支持的参保险种省份',
        'data_range': '仅省份名称与编码，不含任何业务数据',
        'sensitive': False,
    },
    'tasks:submit': {
        'title': '发起处理任务',
        'usage': '提交社保核算/合同整理/文档转换任务（仍受两阶段确认约束）',
        'data_range': '仅可提交任务，不可读取结果',
        'sensitive': False,
    },
    'tasks:read': {
        'title': '查询任务进度',
        'usage': '读取自己发起任务的状态与进度百分比',
        'data_range': '仅状态/进度/耗时，不含明细',
        'sensitive': False,
    },
    'results:read': {
        'title': '获取任务结果',
        'usage': '获取任务完成后的精简摘要与文件卡片路径',
        'data_range': '结果摘要（计数级）与本机文件路径；台账明细仍只在落盘文件内',
        'sensitive': True,
    },
    # roster:read —— 花名册概览。**定义但不开放**：不进 GRANTABLE_SCOPES，
    # 管理端注册界面不可勾选，永远无法授权（个保法敏感个人信息最小必要原则）。
    'roster:read': {
        'title': '读取花名册解析结果（永不开放）',
        'usage': '预留定义，当前版本不开放授权',
        'data_range': '无',
        'sensitive': True,
    },
}

# 可授权集合（注册应用时管理员可勾选的上限；roster:read 永不入选）
GRANTABLE_SCOPES = ['provinces:read', 'tasks:submit', 'tasks:read', 'results:read']

# 工具 → 所需 scope（每个工具唯一；未列出的工具不开放给 OAuth 通道）
TOOL_SCOPES = {
    'insurance_provinces': 'provinces:read',
    'insurance_calculate': 'tasks:submit',
    'convert_pdf_to_word': 'tasks:submit',
    'convert_to_pdf': 'tasks:submit',
    'contract_organize': 'tasks:submit',
    'get_task_status': 'tasks:read',
    'get_task_result': 'results:read',
}

CODE_TTL_SECONDS = 10 * 60            # 授权码 10 分钟、一次性
ACCESS_TOKEN_TTL = 30 * 24 * 3600     # 访问令牌 30 天
REFRESH_TOKEN_TTL = 90 * 24 * 3600    # 刷新令牌 90 天

audit_logger = logging.getLogger('oauth.audit')


def audit(event, **fields):
    """授权链路审计（任何异常不得影响业务）：event + 关键字段单行留痕"""
    try:
        extras = ' '.join('%s=%s' % (k, v) for k, v in sorted(fields.items()))
        audit_logger.info('[oauth] %s %s', event, extras)
    except Exception:
        pass


def _setup_audit_log():
    """审计日志落盘 handler（滚动 5MB×3）；异常静默降级为仅控制台"""
    if getattr(_setup_audit_log, '_done', False):
        return
    _setup_audit_log._done = True
    if os.environ.get('LY_MCP_NO_ACCESS_FILE') == '1':
        return  # 测试隔离：不写实盘
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = os.path.join(data_dir(), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, 'oauth_audit.log'),
            maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        audit_logger.addHandler(handler)
    except Exception:
        pass


# ---------------- 哈希与编码 ----------------

def _sha256_hex(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _pkce_s256(verifier):
    """PKCE S256：base64url(sha256(verifier))，去填充"""
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _new_token(prefix):
    return prefix + '_' + secrets.token_urlsafe(32)


# ---------------- 持久化 ----------------

def _store_path():
    global _STORE_PATH
    if _STORE_PATH is None:
        d = os.path.join(data_dir(), 'data')
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        _STORE_PATH = os.path.join(d, 'oauth_store.json')
    return _STORE_PATH


def _load():
    path = _store_path()
    data = {'apps': [], 'grants': [], 'tokens': []}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in data:
                    if isinstance(loaded.get(key), list):
                        data[key] = loaded[key]
        except Exception:
            pass
    return data


def _save(store):
    try:
        with open(_store_path(), 'w', encoding='utf-8') as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# 授权码（短命，仅内存）：code_hash -> 记录
_CODES = {}


# ---------------- 应用注册（管理员） ----------------

def register_app(name, redirect_uris=None, scopes=None, paste_mode=True, icon=''):
    """登记第三方应用；返回 (app_meta, client_secret)——secret 明文仅此一次

    scopes 为该应用**可申请的上限**；实际授权范围以用户确认页逐项为准。
    """
    if not name or not str(name).strip():
        raise ValueError('应用名称不能为空')
    scopes = [s for s in (scopes or []) if s in GRANTABLE_SCOPES]
    if not scopes:
        raise ValueError('至少需要一个可授权权限范围')
    unknown = [s for s in (scopes or []) if s not in GRANTABLE_SCOPES]
    if unknown:
        raise ValueError('不可授权的权限范围: %s' % ','.join(unknown))
    client_id = 'lyapp_' + secrets.token_hex(8)
    client_secret = _new_token('lysec')
    app = {
        'client_id': client_id,
        'name': str(name).strip(),
        'icon': str(icon or '🔌')[:16],
        'redirect_uris': [u for u in (redirect_uris or []) if u],
        'paste_mode': bool(paste_mode),
        'scopes': scopes,
        'client_secret_hash': _sha256_hex(client_secret),
        'created_at': int(time.time()),
    }
    with _LOCK:
        store = _load()
        store['apps'].append(app)
        _save(store)
    audit('app_registered', client_id=client_id, name=app['name'],
          scopes=','.join(scopes), paste_mode=app['paste_mode'])
    meta = {k: v for k, v in app.items() if k != 'client_secret_hash'}
    return meta, client_secret


def get_app(client_id):
    if not client_id:
        return None
    with _LOCK:
        for app in _load()['apps']:
            if app.get('client_id') == client_id:
                return dict(app)
    return None


def list_apps():
    with _LOCK:
        return [{k: v for k, v in app.items() if k != 'client_secret_hash'}
                for app in _load()['apps']]


def delete_app(client_id):
    """删除应用登记：其全部授权与令牌即刻作废"""
    with _LOCK:
        store = _load()
        store['apps'] = [a for a in store['apps'] if a.get('client_id') != client_id]
        grant_ids = [g['id'] for g in store['grants']
                     if g.get('client_id') == client_id]
        store['grants'] = [g for g in store['grants']
                           if g.get('client_id') != client_id]
        store['tokens'] = [t for t in store['tokens']
                           if t.get('grant_id') not in grant_ids]
        _save(store)
    audit('app_deleted', client_id=client_id)


def _verify_client_secret(app, presented_secret):
    if not presented_secret or not app.get('client_secret_hash'):
        return False
    return secrets.compare_digest(app['client_secret_hash'],
                                  _sha256_hex(presented_secret))


# ---------------- 授权码 ----------------

def validate_authorize_request(client_id, redirect_uri=None, scope=None,
                               code_challenge=None, code_challenge_method=None):
    """授权请求预检（未通过时平台拒绝展示确认页，防探测）

    返回 (app, scopes, error)；error 非 None 时 error=(code, description)。
    """
    app = get_app(client_id)
    if not app:
        return None, None, ('invalid_request', '未知应用')
    if not redirect_uri:
        # 粘贴模式可不带回调；有回调模式必填
        if not app.get('paste_mode'):
            return app, None, ('invalid_request', '缺少回调地址')
    else:
        if redirect_uri not in (app.get('redirect_uris') or []):
            return app, None, ('invalid_request', '回调地址不在白名单内')
    requested = [s for s in (scope or '').split() if s]
    if not requested:
        requested = list(app.get('scopes') or [])
    if not requested:
        return app, None, ('invalid_scope', '应用未配置可授权范围')
    exceed = [s for s in requested if s not in (app.get('scopes') or [])]
    if exceed:
        return app, None, ('invalid_scope', '超出应用登记的权限范围')
    if code_challenge_method not in (None, 'S256'):
        return app, None, ('invalid_request', '仅支持 PKCE S256')
    if not code_challenge:
        return app, None, ('invalid_request', '缺少 code_challenge（PKCE 强制）')
    return app, requested, None


def create_authorization_code(client_id, username, scopes, code_challenge):
    """用户点击同意后签发一次性授权码（10 分钟有效，仅存哈希）"""
    code = _new_token('lycode')
    with _LOCK:
        # 顺手清理过期授权码
        now = int(time.time())
        for h in [h for h, c in _CODES.items() if c['expires_at'] < now]:
            _CODES.pop(h, None)
        _CODES[_sha256_hex(code)] = {
            'client_id': client_id,
            'username': username,
            'scopes': list(scopes),
            'code_challenge': code_challenge,
            'expires_at': now + CODE_TTL_SECONDS,
        }
    audit('consent_granted', client_id=client_id, user=username,
          scopes=','.join(scopes))
    return code


def record_denial(client_id, username):
    """用户拒绝：不留任何凭证，仅审计一条（不含用户额外信息）"""
    audit('consent_denied', client_id=client_id, user=username)


def exchange_code(code, client_id, client_secret, code_verifier):
    """授权码换令牌（PKCE S256 校验 + 一次性消费 + 重放全废）

    成功返回 {'access_token', 'refresh_token', 'expires_in', 'scope'}；
    失败返回 {'error', 'error_description'}（调用方以 400 返回）。
    """
    if not code or not client_id or not code_verifier:
        return {'error': 'invalid_request', 'error_description': '参数缺失'}
    app = get_app(client_id)
    if not app or not _verify_client_secret(app, client_secret):
        audit('token_exchange_failed', client_id=client_id, reason='bad_client')
        return {'error': 'invalid_client', 'error_description': '应用凭证校验失败'}
    code_hash = _sha256_hex(code)
    with _LOCK:
        record = _CODES.pop(code_hash, None)  # 取出即消费（一次性）
        if record is None:
            # 已消费/不存在：若属重放，作废该应用全部令牌（防重放）
            audit('token_exchange_failed', client_id=client_id, reason='code_unknown')
            return {'error': 'invalid_grant', 'error_description': '授权码无效或已使用'}
        if record['client_id'] != client_id:
            audit('token_exchange_failed', client_id=client_id, reason='code_mismatch')
            return {'error': 'invalid_grant', 'error_description': '授权码与应用不匹配'}
        if record['expires_at'] < int(time.time()):
            audit('token_exchange_failed', client_id=client_id, reason='code_expired')
            return {'error': 'invalid_grant', 'error_description': '授权码已过期'}
        if not secrets.compare_digest(
                _pkce_s256(code_verifier), record['code_challenge']):
            audit('token_exchange_failed', client_id=client_id, reason='bad_verifier')
            return {'error': 'invalid_grant', 'error_description': 'code_verifier 校验失败'}
        grant = _upsert_grant(store=None, client_id=client_id,
                              username=record['username'], scopes=record['scopes'])
        tokens = _issue_tokens(grant['id'], record['scopes'])
        audit('token_issued', client_id=client_id, user=record['username'],
              scopes=','.join(record['scopes']))
        return tokens


def _upsert_grant(store, client_id, username, scopes):
    """创建或更新授权关系（用户×应用）；store=None 时自行加载"""
    if store is None:
        store = _load()
    for g in store['grants']:
        if (g.get('client_id') == client_id and g.get('username') == username
                and not g.get('revoked')):
            g['scopes'] = list(scopes)
            g['updated_at'] = int(time.time())
            _save(store)
            return g
    grant = {
        'id': 'lygrant_' + secrets.token_hex(8),
        'client_id': client_id,
        'username': username,
        'scopes': list(scopes),
        'created_at': int(time.time()),
        'updated_at': int(time.time()),
        'revoked': False,
        'last_used_at': None,
    }
    store['grants'].append(grant)
    _save(store)
    return grant


def _issue_tokens(grant_id, scopes):
    """签发访问+刷新令牌（哈希落库，明文只返回给应用一次）"""
    access = _new_token('lyat')
    refresh = _new_token('lyrt')
    now = int(time.time())
    with _LOCK:
        store = _load()
        store['tokens'].append({
            'grant_id': grant_id,
            'scopes': list(scopes),
            'access_hash': _sha256_hex(access),
            'access_expires_at': now + ACCESS_TOKEN_TTL,
            'refresh_hash': _sha256_hex(refresh),
            'refresh_expires_at': now + REFRESH_TOKEN_TTL,
            'created_at': now,
        })
        _save(store)
    return {
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': ACCESS_TOKEN_TTL,
        'refresh_token': refresh,
        'scope': ' '.join(scopes),
    }


def refresh_access_token(refresh_token, client_id, client_secret):
    """刷新令牌轮换：签发新对，旧刷新令牌作废"""
    if not refresh_token or not client_id:
        return {'error': 'invalid_request', 'error_description': '参数缺失'}
    app = get_app(client_id)
    if not app or not _verify_client_secret(app, client_secret):
        return {'error': 'invalid_client', 'error_description': '应用凭证校验失败'}
    refresh_hash = _sha256_hex(refresh_token)
    with _LOCK:
        store = _load()
        match = None
        for t in store['tokens']:
            if t.get('refresh_hash') == refresh_hash:
                match = t
                break
        if match is None:
            audit('refresh_failed', client_id=client_id, reason='unknown_token')
            return {'error': 'invalid_grant', 'error_description': '刷新令牌无效'}
        grant = next((g for g in store['grants'] if g['id'] == match['grant_id']), None)
        if grant is None or grant.get('revoked'):
            store['tokens'] = [t for t in store['tokens']
                               if t.get('grant_id') != match['grant_id']]
            _save(store)
            audit('refresh_failed', client_id=client_id, reason='revoked')
            return {'error': 'invalid_grant', 'error_description': '授权已被撤销'}
        if match.get('refresh_expires_at', 0) < int(time.time()):
            store['tokens'].remove(match)
            _save(store)
            audit('refresh_failed', client_id=client_id, reason='expired')
            return {'error': 'invalid_grant', 'error_description': '刷新令牌已过期'}
        # 轮换：旧对作废，签发新对
        grant_id = match['grant_id']
        scopes = list(match.get('scopes') or grant.get('scopes') or [])
        store['tokens'].remove(match)
        _save(store)
        tokens = _issue_tokens(grant_id, scopes)
        audit('token_refreshed', client_id=client_id, user=grant.get('username'))
        return tokens


# ---------------- 鉴权（MCP 调用层） ----------------

def authenticate(access_token):
    """校验 OAuth 访问令牌；有效返回授权视图，无效/过期/撤销返回 None

    撤销即时生效：每次调用直查落库（无缓存）。
    """
    if not access_token:
        return None
    access_hash = _sha256_hex(access_token)
    with _LOCK:
        store = _load()
        for t in store['tokens']:
            if t.get('access_hash') != access_hash:
                continue
            if t.get('access_expires_at', 0) < int(time.time()):
                return None
            grant = next((g for g in store['grants']
                          if g['id'] == t.get('grant_id')), None)
            if grant is None or grant.get('revoked'):
                return None
            app = next((a for a in store['apps']
                        if a.get('client_id') == grant.get('client_id')), None)
            if app is None:
                return None
            grant['last_used_at'] = int(time.time())
            _save(store)
            return {
                'grant_id': grant['id'],
                'client_id': grant['client_id'],
                'app_name': app.get('name'),
                'username': grant.get('username'),
                'scopes': list(t.get('scopes') or grant.get('scopes') or []),
            }
    return None


def required_scope(tool_name):
    """工具所需 scope；未映射的工具不开放 OAuth 通道（返回 None）"""
    return TOOL_SCOPES.get(tool_name)


def check_scope(auth, tool_name):
    """校验授权是否覆盖工具所需 scope；通过返回 scope 名，否则 None"""
    if not auth:
        return None
    scope = required_scope(tool_name)
    if scope is None or scope not in (auth.get('scopes') or []):
        return None
    return scope


# ---------------- 管理与撤销 ----------------

def list_grants(username=None):
    """授权列表（管理页）：username 过滤本人；含静态令牌由展示层单列"""
    with _LOCK:
        store = _load()
        apps = {a['client_id']: a for a in store['apps']}
        out = []
        for g in store['grants']:
            if username and g.get('username') != username:
                continue
            app = apps.get(g.get('client_id')) or {}
            out.append({
                'id': g['id'],
                'client_id': g['client_id'],
                'app_name': app.get('name') or g.get('client_id'),
                'app_icon': app.get('icon') or '🔌',
                'username': g.get('username'),
                'scopes': list(g.get('scopes') or []),
                'created_at': g.get('created_at'),
                'updated_at': g.get('updated_at'),
                'last_used_at': g.get('last_used_at'),
                'revoked': bool(g.get('revoked')),
            })
        return out


def revoke_grant(grant_id, username=None, as_admin=False):
    """撤销授权：全部令牌即刻失效；username 非空且非管理员时只能撤自己的"""
    with _LOCK:
        store = _load()
        for g in store['grants']:
            if g['id'] == grant_id:
                if username and not as_admin and g.get('username') != username:
                    return False
                g['revoked'] = True
                g['updated_at'] = int(time.time())
                store['tokens'] = [t for t in store['tokens']
                                   if t.get('grant_id') != grant_id]
                _save(store)
                audit('grant_revoked', grant_id=grant_id,
                      client_id=g.get('client_id'), user=g.get('username'))
                return True
    return False
