# -*- coding: utf-8 -*-
"""用户认证模块 - 支持本地模式和远程云端模式"""
import os
import sys
import json
import sqlite3
import secrets
import hashlib
import urllib.request
import urllib.error
from datetime import datetime
from functools import wraps

from flask import session, redirect, url_for, jsonify, request

# ============ 远程认证配置 ============
# 部署 auth_server.py 到 Render.com 后，将下面的 URL 改为实际地址
# 示例：AUTH_SERVER_URL = 'https://luyue-auth.onrender.com'
# 也可以在 EXE 同目录下创建 auth_config.json 文件来配置
# auth_config.json 格式：{"auth_server_url": "https://xxx.onrender.com"}
AUTH_SERVER_URL = None  # None = 使用本地模式；设置 URL = 使用远程模式
AUTH_API_KEY = 'ly_qyfw_2026_auth_key'  # 与 auth_server.py 中 API_KEY 一致

# 尝试从配置文件加载远程认证地址
def _load_auth_config():
    global AUTH_SERVER_URL, AUTH_API_KEY
    config_paths = []
    if getattr(sys, 'frozen', False):
        config_paths.append(os.path.join(os.path.dirname(sys.executable), 'auth_config.json'))
    else:
        config_paths.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'auth_config.json'))
    config_paths.append(os.path.join(os.path.expanduser('~'), '.luyue_auth_config.json'))

    for cfg_path in config_paths:
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                if cfg.get('auth_server_url'):
                    AUTH_SERVER_URL = cfg['auth_server_url']
                    import logging
                    logging.getLogger('platform').info(f'已加载远程认证配置: {AUTH_SERVER_URL}')
                if cfg.get('auth_api_key'):
                    AUTH_API_KEY = cfg['auth_api_key']
                break
            except Exception:
                pass

_load_auth_config()

# 数据库路径（本地模式使用）
if getattr(sys, 'frozen', False):
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(sys.executable), 'data')
else:
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

DB_PATH = os.path.join(_DEFAULT_DATA_DIR, 'users.db')

# 远程模式下的 token 存储键名
_SESSION_TOKEN_KEY = '_auth_token'

# 最近一次远程请求返回的 token（用于 register/login 后传递给调用方）
_LAST_REMOTE_TOKEN = None

# ============ 远程 API 调用辅助 ============

def _is_remote():
    """判断当前是否为远程模式"""
    return bool(AUTH_SERVER_URL)


def _remote_request(endpoint, data=None, method='POST', token=None):
    """调用远程认证服务器 API"""
    global _LAST_REMOTE_TOKEN
    url = AUTH_SERVER_URL.rstrip('/') + endpoint
    headers = {
        'X-API-Key': AUTH_API_KEY,
        'Content-Type': 'application/json',
    }
    if token:
        headers['X-User-Token'] = token

    body = json.dumps(data).encode('utf-8') if data else None

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            # 保存返回的 token（注册/登录时使用）
            if result.get('token'):
                _LAST_REMOTE_TOKEN = result['token']
            return result, resp.status
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode('utf-8'))
            return err_body, e.code
        except Exception:
            return {'error': f'服务器错误 ({e.code})'}, e.code
    except urllib.error.URLError as e:
        import logging
        logging.getLogger('platform').warning(f'远程认证连接失败: {e}')
        return {'error': '无法连接认证服务器，请检查网络'}, 503
    except Exception as e:
        import logging
        logging.getLogger('platform').warning(f'远程认证异常: {e}')
        return {'error': f'认证服务异常: {e}'}, 500


def get_remote_token():
    """获取最近一次远程注册/登录返回的 API token（取出后清空）"""
    global _LAST_REMOTE_TOKEN
    token = _LAST_REMOTE_TOKEN
    _LAST_REMOTE_TOKEN = None
    return token


def get_db():
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 预置邀请码已废弃 —— 改为管理员动态生成，仅保留列表引用供兼容
PRESET_INVITE_CODES = []


def init_db(data_dir=None):
    """初始化数据库表。data_dir: 自定义数据目录路径"""
    global DB_PATH
    if data_dir:
        DB_PATH = os.path.join(data_dir, 'users.db')
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )''')
    # 兼容旧表：如果 is_active 列不存在则添加
    try:
        c.execute('SELECT is_active FROM users LIMIT 1')
    except sqlite3.OperationalError:
        c.execute('ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1')
    c.execute('''CREATE TABLE IF NOT EXISTS invite_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        created_by INTEGER,
        used_by INTEGER,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        used_at TEXT,
        FOREIGN KEY (created_by) REFERENCES users(id),
        FOREIGN KEY (used_by) REFERENCES users(id)
    )''')
    # 兼容旧表：如果 note 列不存在则添加
    try:
        c.execute('SELECT note FROM invite_codes LIMIT 1')
    except sqlite3.OperationalError:
        c.execute('ALTER TABLE invite_codes ADD COLUMN note TEXT DEFAULT \'\'')
    conn.commit()
    conn.close()


def _hash_password(password):
    """SHA-256哈希密码（加固定salt）"""
    salt = 'ly_qyfw_2026'
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest()


def create_user(username, password, is_admin=False, invite_code=''):
    """创建用户，返回(user_id, error)"""
    if _is_remote():
        # 远程模式���注册走远程 API（由远程服务器决定 is_admin）
        payload = {'username': username, 'password': password}
        if invite_code:
            payload['invite_code'] = invite_code
        result, status = _remote_request('/api/auth/register', payload)
        if status == 200 and result.get('ok'):
            return result.get('user_id'), None
        return None, result.get('error', '注册失败')
    # 本地模式
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)',
            (username, _hash_password(password), 1 if is_admin else 0,
             datetime.now().isoformat())
        )
        conn.commit()
        uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        return uid, None
    except sqlite3.IntegrityError:
        return None, '用户名已存在'
    finally:
        conn.close()


def verify_user(username, password):
    """验证用户登录，返回(user_dict, error)"""
    if _is_remote():
        result, status = _remote_request('/api/auth/login', {
            'username': username,
            'password': password,
        })
        if status == 200 and result.get('ok'):
            token = result.get('token', '')
            return {
                'id': result.get('user_id'),
                'username': result.get('username'),
                'is_admin': result.get('is_admin', False),
                'is_active': True,
                '_token': token,
            }, None
        return None, result.get('error', '登录失败')
    # 本地模式
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM users WHERE username = ?', (username,)
    ).fetchone()
    conn.close()
    if not row:
        return None, '用户名不存在'
    if row['password_hash'] != _hash_password(password):
        return None, '密码错误'
    if not row['is_active']:
        return None, '账号已被停用，请联系管理员'
    return dict(row), None


def get_user_count():
    """获取用户总数（用于判断是否首次注册）"""
    if _is_remote():
        result, status = _remote_request('/api/auth/user_count', method='GET')
        if status == 200:
            return result.get('count', 0)
        # 远程不可用时返回一个很大的数，强制走邀请码注册
        return 999
    conn = get_db()
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count


def generate_invite_code(created_by, note='', count=1):
    """生成邀请码，返回单个code（count=1）或codes列表（count>1）。created_by 为管理员用户ID"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request('/api/auth/generate_invite',
                                          {'note': note, 'count': count}, token=token)
        if status == 200:
            codes = result.get('codes', [])
            return codes[0] if len(codes) == 1 else codes
        return None
    codes = []
    conn = get_db()
    for _ in range(count):
        code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:8].upper()
        conn.execute(
            'INSERT INTO invite_codes (code, created_by, created_at, note) VALUES (?, ?, ?, ?)',
            (code, created_by, datetime.now().isoformat(), note)
        )
        codes.append(code)
    conn.commit()
    conn.close()
    return codes[0] if len(codes) == 1 else codes


def validate_invite_code(code):
    """验证邀请码是否有效（存在且未被使用）"""
    if _is_remote():
        result, status = _remote_request('/api/auth/validate_invite', {'code': code})
        return status == 200 and result.get('valid', False)
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM invite_codes WHERE code = ? AND used_by IS NULL',
        (code,)
    ).fetchone()
    conn.close()
    return row is not None


def consume_invite_code(code, user_id):
    """标记邀请码已使用"""
    if _is_remote():
        return  # 远程模式下消费已在注册时由服务器完成
    conn = get_db()
    conn.execute(
        'UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ?',
        (user_id, datetime.now().isoformat(), code)
    )
    conn.commit()
    conn.close()


def get_invite_codes(created_by=None):
    """获取邀请码列表"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request('/api/auth/invite_codes', method='GET', token=token)
        if status == 200:
            return result.get('codes', [])
        return []
    conn = get_db()
    if created_by:
        rows = conn.execute(
            '''SELECT ic.*, u.username as created_by_name
               FROM invite_codes ic LEFT JOIN users u ON ic.created_by = u.id
               WHERE ic.created_by = ? ORDER BY ic.created_at DESC''',
            (created_by,)
        ).fetchall()
    else:
        rows = conn.execute(
            '''SELECT ic.*, u.username as created_by_name, u2.username as used_by_name
               FROM invite_codes ic
               LEFT JOIN users u ON ic.created_by = u.id
               LEFT JOIN users u2 ON ic.used_by = u.id
               ORDER BY ic.created_at DESC'''
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_users():
    """获取所有用户列表"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request('/api/auth/users', method='GET', token=token)
        if status == 200:
            return result.get('users', [])
        return []
    conn = get_db()
    rows = conn.execute(
        'SELECT id, username, is_admin, is_active, created_at FROM users ORDER BY id'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_user_active(user_id):
    """切换用户启用/停用状态，返回新状态"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/toggle', token=token)
        if status == 200:
            return result.get('is_active', True)
        return None
    conn = get_db()
    row = conn.execute('SELECT is_active FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row:
        conn.close()
        return None
    new_state = 0 if row['is_active'] else 1
    conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_state, user_id))
    conn.commit()
    conn.close()
    return bool(new_state)


def delete_user(user_id):
    """删除用户"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/delete', token=token)
        return status == 200
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()


def reset_password(user_id, new_password):
    """重置用户密码"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/reset_password',
                                          {'password': new_password}, token=token)
        return status == 200
    conn = get_db()
    conn.execute(
        'UPDATE users SET password_hash = ? WHERE id = ?',
        (_hash_password(new_password), user_id)
    )
    conn.commit()
    conn.close()


def get_user_by_id(user_id):
    """根据ID获取用户"""
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}', method='GET', token=token)
        if status == 200:
            return result.get('user', None)
        return None
    conn = get_db()
    row = conn.execute(
        'SELECT id, username, is_admin, is_active, created_at FROM users WHERE id = ?',
        (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ============ Flask 装饰器 ============
def login_required(f):
    """要求登录才能访问"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            # 如果是API请求返回JSON，否则重定向
            if request.path.startswith('/api/'):
                return jsonify({'error': '请先登录', 'need_login': True}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """要求管理员权限"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': '请先登录', 'need_login': True}), 401
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated
