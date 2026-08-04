# -*- coding: utf-8 -*-
"""用户认证模块 - 支持本地模式和远程云端模式

远程模式下，所有操作优先走远程API；远程失败时自动回退到本地SQLite数据库，
确保软件在远程服务器不可用时仍能正常工作（邀请码生成、用户注册、登录等）。
"""
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


def _log_fallback(operation, status, error=''):
    """记录远程操作失败并回退到本地的日志"""
    import logging
    logging.getLogger('platform').warning(
        f'远程{operation}失败({status}): {error}，回退到本地模式')


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


# 预置邀请码列表（软件分发到各电脑后，首次初始化数据库时统一插入）
# 所有电脑共享同一套邀请码，管理员可查看并分发给用户
PRESET_INVITE_CODES = [
    'LYA3K7M2', 'LYB8X4N6', 'LYC2P9Q5', 'LYD6R1S3', 'LYE5T7U8',
    'LYF4V2W9', 'LYG9X3Y1', 'LYH7Z5A2', 'LYI1B6C4', 'LYJ3D8E7',
    'LYK5F2G9', 'LYL8H4J1', 'LYM6K3S7', 'LYN2L5T8', 'LYO9Q4U6',
    'LYP7R1V3', 'LYQ4S9W2', 'LYR8T5X1', 'LYS3U6Y4', 'LYT6V7Z8',
    'LYU2W9A3', 'LYV5X4B1', 'LYW8Y6C7', 'LYX3Z9D2', 'LYY7A4E5',
    'LYZ1B8F6', 'LYA9C3G2', 'LYB4D7H5', 'LYC6E1J8', 'LYD8F3K4',
    'LYE2G7L5', 'LYF5H9M1', 'LYG7J3N6', 'LYH1K8Q4', 'LYI3L5R2',
    'LYJ9M7S4', 'LYK4N1T8', 'LYL6Q3U5', 'LYM8R7V2', 'LYN2S9W4',
    'LYO5T1X6', 'LYP7U3Y8', 'LYQ9V5Z1', 'LYR2W7A4', 'LYS4X9B6',
    'LYT6Y3C8', 'LYU8Z5D1', 'LYV1A7E4', 'LYW3B9F6', 'LYX5C2G8',
]


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

    # 预置邀请码：插入缺失的预置邀请码（确保所有电脑都有同一套邀请码）
    existing_count = c.execute('SELECT COUNT(*) FROM invite_codes').fetchone()[0]
    inserted = 0
    now = datetime.now().isoformat()
    for code in PRESET_INVITE_CODES:
        # 逐个检查是否已存在，不存在则插入
        row = c.execute('SELECT id FROM invite_codes WHERE code = ?', (code,)).fetchone()
        if row is None:
            c.execute(
                'INSERT OR IGNORE INTO invite_codes (code, created_by, created_at, note) VALUES (?, NULL, ?, ?)',
                (code, now, '系统预置')
            )
            inserted += 1
    if inserted > 0:
        conn.commit()
        import logging
        logging.getLogger('platform').info(f'已预置 {inserted} 个邀请码（共 {len(PRESET_INVITE_CODES)} 个）')

    conn.close()


def _hash_password(password):
    """SHA-256哈希密码（加固定salt）"""
    salt = 'ly_qyfw_2026'
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest()


def create_user(username, password, is_admin=False, invite_code=''):
    """创建用户，返回(user_id, error)

    远程模式：优先走远程API注册；远程失败时若邀请码在本地有效，回退到本地注册。
    本地模式：直接在本地数据库创建用户。
    """
    if _is_remote():
        payload = {'username': username, 'password': password}
        if invite_code:
            payload['invite_code'] = invite_code
        result, status = _remote_request('/api/auth/register', payload)
        if status == 200 and result.get('ok'):
            return result.get('user_id'), None
        # 远程注册失败：尝试本地回退
        conn = get_db()
        if invite_code:
            # 非首次注册：检查邀请码是否在本地有效
            local_row = conn.execute(
                'SELECT id FROM invite_codes WHERE code = ? AND used_by IS NULL',
                (invite_code,)
            ).fetchone()
            if local_row:
                _log_fallback('注册', status, result.get('error', ''))
                try:
                    conn.execute(
                        'INSERT INTO users (username, password_hash, is_admin, created_at) '
                        'VALUES (?, ?, ?, ?)',
                        (username, _hash_password(password), 0,
                         datetime.now().isoformat())
                    )
                    conn.commit()
                    uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    conn.close()
                    return uid, None
                except sqlite3.IntegrityError:
                    conn.close()
                    return None, '用户名已存在'
                except Exception as e:
                    conn.close()
                    return None, f'本地注册失败: {e}'
        else:
            # 首次注册（无邀请码）：如果本地无用户，允许本地注册为管理员
            local_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            if local_count == 0:
                _log_fallback('注册', status, result.get('error', ''))
                try:
                    conn.execute(
                        'INSERT INTO users (username, password_hash, is_admin, created_at) '
                        'VALUES (?, ?, ?, ?)',
                        (username, _hash_password(password), 1,
                         datetime.now().isoformat())
                    )
                    conn.commit()
                    uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    conn.close()
                    return uid, None
                except sqlite3.IntegrityError:
                    conn.close()
                    return None, '用户名已存在'
                except Exception as e:
                    conn.close()
                    return None, f'本地注册失败: {e}'
        conn.close()
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
    """验证用户登录，返回(user_dict, error)

    远程模式：优先走远程API登录；远程失败时回退到本地数据库验证。
    本地模式：直接在本地数据库验证。
    """
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
        # 远程登录失败：回退到本地验证（用户可能在远程不可用时本地注册）
        _log_fallback('登录', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM users WHERE username = ?', (username,)
    ).fetchone()
    conn.close()
    if not row:
        return None, '用户名或密码错误'
    if row['password_hash'] != _hash_password(password):
        return None, '用户名或密码错误'
    if not row['is_active']:
        return None, '账号已被停用，请联系管理员'
    return dict(row), None


def get_user_count():
    """获取用户总数（用于判断是否首次注册）

    远程模式：取远程和本地用户数的最大值，避免远程数据丢失导致误判首次注册。
    """
    if _is_remote():
        result, status = _remote_request('/api/auth/user_count', method='GET')
        if status == 200:
            remote_count = result.get('count', 0)
            # 取远程和本地的最大值，确保不会因远程数据丢失而误判为首次注册
            conn = get_db()
            local_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            conn.close()
            return max(remote_count, local_count)
        # 远程不可用时回退到本地
        _log_fallback('获取用户数', status, result.get('error', ''))
    conn = get_db()
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count


def generate_invite_code(created_by, note=''):
    """生成邀请码。created_by 为管理员用户ID

    远程模式：优先走远程API生成；远程失败时回退到本地数据库生成。
    本地模式：直接在本地数据库生成。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        if token:
            result, status = _remote_request('/api/auth/generate_invite',
                                              {'note': note}, token=token)
            if status == 200:
                # 远程可能返回 {'code': 'XXX'} 或 {'codes': ['XXX', ...]}
                code = result.get('code', '')
                if not code:
                    codes_list = result.get('codes', [])
                    if codes_list and isinstance(codes_list, list):
                        code = codes_list[0]
                if code:
                    return code
                # 远程返回200但无有效邀请码，回退到本地
                _log_fallback('生成邀请码', status, '远程返回无有效邀请码')
            else:
                _log_fallback('生成邀请码', status, result.get('error', ''))
        else:
            _log_fallback('生成邀请码', '无token', 'session中无auth_token')
    # 本地模式（或远程失败的回退）
    try:
        code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:8].upper()
        conn = get_db()
        conn.execute(
            'INSERT INTO invite_codes (code, created_by, created_at, note) VALUES (?, ?, ?, ?)',
            (code, created_by, datetime.now().isoformat(), note)
        )
        conn.commit()
        conn.close()
        return code
    except Exception as e:
        _log_fallback('生成邀请码(本地)', '异常', str(e))
        return ''


def validate_invite_code(code):
    """验证邀请码是否有效（存在且未被使用）

    远程模式：优先走远程API验证；远程验证无效时也检查本地数据库
    （可能是远程不可用时本地生成的邀请码）。
    """
    if _is_remote():
        result, status = _remote_request('/api/auth/validate_invite', {'code': code})
        if status == 200:
            if result.get('valid', False):
                return True
            # 远程说无效：可能是已被远程使用，也可能是远程数据库没有此码
            # 检查本地数据库（可能是远程不可用时本地生成的）
        else:
            # 远程请求失败（服务器错误等）：回退到本地
            _log_fallback('验证邀请码', status, result.get('error', ''))
    # 本地验证（本地模式，或远程失败/远程无效时的回退）
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM invite_codes WHERE code = ? AND used_by IS NULL',
        (code,)
    ).fetchone()
    conn.close()
    return row is not None


def consume_invite_code(code, user_id):
    """标记邀请码已使用

    无论远程还是本地模式，都在本地数据库标记一次。
    远程模式下远程消费已在注册时由服务器完成，本地标记确保一致性。
    """
    conn = get_db()
    conn.execute(
        'UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL',
        (user_id, datetime.now().isoformat(), code)
    )
    conn.commit()
    conn.close()


def get_invite_codes(created_by=None):
    """获取邀请码列表

    远程模式：优先走远程API；远程失败时回退到本地数据库。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request('/api/auth/invite_codes', method='GET', token=token)
        if status == 200:
            return result.get('codes', [])
        _log_fallback('获取邀请码列表', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
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
    """获取所有用户列表

    远程模式：优先走远程API；远程失败时回退到本地数据库。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request('/api/auth/users', method='GET', token=token)
        if status == 200:
            return result.get('users', [])
        _log_fallback('获取用户列表', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
    conn = get_db()
    rows = conn.execute(
        'SELECT id, username, is_admin, is_active, created_at FROM users ORDER BY id'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_user_active(user_id):
    """切换用户启用/停用状态，返回新状态

    远程模式：优先走远程API；远程失败时回退到本地数据库。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/toggle', token=token)
        if status == 200:
            return result.get('is_active', True)
        _log_fallback('切换用户状态', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
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
    """删除用户

    远程模式：优先走远程API；远程失败时回退到本地数据库。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/delete', token=token)
        if status == 200:
            return True
        _log_fallback('删除用户', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return True


def reset_password(user_id, new_password):
    """重置用户密码

    远程模式：优先走远程API；远程失败时回退到本地数据库。
    """
    if _is_remote():
        token = session.get(_SESSION_TOKEN_KEY, '')
        result, status = _remote_request(f'/api/auth/users/{user_id}/reset_password',
                                          {'password': new_password}, token=token)
        if status == 200:
            return True
        _log_fallback('重置密码', status, result.get('error', ''))
    # 本地模式（或远程失败的回退）
    conn = get_db()
    conn.execute(
        'UPDATE users SET password_hash = ? WHERE id = ?',
        (_hash_password(new_password), user_id)
    )
    conn.commit()
    conn.close()
    return True


def get_user_by_id(user_id):
    """根据ID获取用户"""
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
