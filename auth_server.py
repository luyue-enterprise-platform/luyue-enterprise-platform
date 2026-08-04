# -*- coding: utf-8 -*-
"""
鲁岳企业服务 - 云端统一认证服务器
部署到 Render.com（免费）后，所有 EXE 客户端统一连接此服务器进行注册和登录。

Render 部署步骤：
1. 在 github.com 创建仓库，上传本项目
2. 在 render.com 注册（GitHub 登录），创建 Web Service
3. Build Command: pip install -r requirements-server.txt
4. Start Command: gunicorn auth_server:app -b 0.0.0.0:$PORT
5. 设置环境变量：API_KEY=你的密钥（与 EXE 中一致）
"""
import os
import sys
import sqlite3
import secrets
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, g

# ============ 配置 ============
API_KEY = os.environ.get('API_KEY', 'ly_qyfw_2026_auth_key')  # 服务端密钥，通过环境变量覆盖
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_server')
DB_PATH = os.path.join(DATA_DIR, 'users.db')

app = Flask(__name__)

# ============ 预置邀请码 ============
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

# Token 有效期（秒）- 用于远程 API 认证后的会话
TOKEN_EXPIRY = 86400 * 7  # 7 天


# ============ 数据库 ============
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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
    try:
        c.execute('SELECT note FROM invite_codes LIMIT 1')
    except sqlite3.OperationalError:
        c.execute('ALTER TABLE invite_codes ADD COLUMN note TEXT DEFAULT \'\'')

    # API tokens 表（用于远程认证后的 token 验证）
    c.execute('''CREATE TABLE IF NOT EXISTS api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    conn.commit()

    # 预置邀请码
    inserted = 0
    now = datetime.now().isoformat()
    for code in PRESET_INVITE_CODES:
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
        logging.info(f'已预置 {inserted} 个邀请码')

    conn.close()


# ============ 密码哈希 ============
def _hash_password(password):
    salt = 'ly_qyfw_2026'
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest()


# ============ API 密钥校验 ============
def require_api_key(f):
    """要求请求头携带正确的 API 密钥"""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key', '')
        if key != API_KEY:
            return jsonify({'error': '未授权的访问'}), 403
        return f(*args, **kwargs)
    return decorated


def require_token(f):
    """要求请求头携带有效的用户 token（用于管理员操作）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-User-Token', '')
        if not token:
            return jsonify({'error': '未登录'}), 401
        conn = get_db()
        row = conn.execute(
            'SELECT t.user_id, u.is_admin FROM api_tokens t '
            'JOIN users u ON t.user_id = u.id WHERE t.token = ?',
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': '登录已过期，请重新登录'}), 401
        g.user_id = row['user_id']
        g.is_admin = bool(row['is_admin'])
        return f(*args, **kwargs)
    return decorated


# ============ API 路由 ============

@app.route('/api/auth/ping', methods=['GET'])
def ping():
    """健康检查"""
    return jsonify({'ok': True, 'version': '1.0'})


# ---- 注册 ----
@app.route('/api/auth/register', methods=['POST'])
@require_api_key
def auth_register():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    invite_code = data.get('invite_code', '').strip()

    if len(username) < 2:
        return jsonify({'error': '用户名至少2位'}), 400
    if len(password) < 6:
        return jsonify({'error': '密码至少6位'}), 400

    conn = get_db()
    try:
        # 判断是否首次注册
        user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]

        if user_count == 0:
            # 首次注册 → 管理员，无需邀请码
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, is_active, created_at) VALUES (?, ?, 1, 1, ?)',
                (username, _hash_password(password), datetime.now().isoformat())
            )
            conn.commit()
            uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            # 生成 token
            token = secrets.token_urlsafe(32)
            conn.execute(
                'INSERT INTO api_tokens (user_id, token, created_at) VALUES (?, ?, ?)',
                (uid, token, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            return jsonify({
                'ok': True,
                'user_id': uid,
                'username': username,
                'is_admin': True,
                'token': token,
                'is_first': True,
            })
        else:
            # 非首次注册 → 必须验证邀请码
            if not invite_code:
                return jsonify({'error': '请输入邀请码'}), 400
            row = conn.execute(
                'SELECT id FROM invite_codes WHERE code = ? AND used_by IS NULL',
                (invite_code,)
            ).fetchone()
            if not row:
                conn.close()
                return jsonify({'error': '邀请码无效或已被使用'}), 400
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, is_active, created_at) VALUES (?, ?, 0, 1, ?)',
                (username, _hash_password(password), datetime.now().isoformat())
            )
            conn.commit()
            uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            # 消费邀请码
            conn.execute(
                'UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ?',
                (uid, datetime.now().isoformat(), invite_code)
            )
            # 生成 token
            token = secrets.token_urlsafe(32)
            conn.execute(
                'INSERT INTO api_tokens (user_id, token, created_at) VALUES (?, ?, ?)',
                (uid, token, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            return jsonify({
                'ok': True,
                'user_id': uid,
                'username': username,
                'is_admin': False,
                'token': token,
                'is_first': False,
            })

    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': '用户名已存在'}), 400


# ---- 登录 ----
@app.route('/api/auth/login', methods=['POST'])
@require_api_key
def auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'error': '请输入用户名和密码'}), 400

    conn = get_db()
    row = conn.execute(
        'SELECT * FROM users WHERE username = ?', (username,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({'error': '用户名或密码错误'}), 401
    if row['password_hash'] != _hash_password(password):
        conn.close()
        return jsonify({'error': '用户名或密码错误'}), 401
    if not row['is_active']:
        conn.close()
        return jsonify({'error': '账号已被停用，请联系管理员'}), 403

    # 生成新 token（旧 token 保留到过期自然失效）
    token = secrets.token_urlsafe(32)
    conn.execute(
        'INSERT INTO api_tokens (user_id, token, created_at) VALUES (?, ?, ?)',
        (row['id'], token, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    return jsonify({
        'ok': True,
        'user_id': row['id'],
        'username': row['username'],
        'is_admin': bool(row['is_admin']),
        'token': token,
    })


# ---- 获取用户数 ----
@app.route('/api/auth/user_count', methods=['GET'])
@require_api_key
def auth_user_count():
    conn = get_db()
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return jsonify({'count': count})


# ---- 验证邀请码 ----
@app.route('/api/auth/validate_invite', methods=['POST'])
@require_api_key
def auth_validate_invite():
    data = request.get_json(silent=True) or {}
    code = data.get('code', '').strip()
    if not code:
        return jsonify({'valid': False, 'error': '请输入邀请码'}), 400
    conn = get_db()
    row = conn.execute(
        'SELECT id FROM invite_codes WHERE code = ? AND used_by IS NULL',
        (code,)
    ).fetchone()
    conn.close()
    return jsonify({'valid': row is not None})


# ---- 修改密码 ----
@app.route('/api/auth/change_password', methods=['POST'])
@require_api_key
@require_token
def auth_change_password():
    data = request.get_json(silent=True) or {}
    old_pwd = data.get('old_password', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': '新密码至少6位'}), 400

    conn = get_db()
    row = conn.execute('SELECT password_hash FROM users WHERE id = ?', (g.user_id,)).fetchone()
    if not row or row['password_hash'] != _hash_password(old_pwd):
        conn.close()
        return jsonify({'error': '旧密码错误'}), 400

    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                 (_hash_password(new_pwd), g.user_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ 管理员 API ============

# ---- 邀请码列表 ----
@app.route('/api/auth/invite_codes', methods=['GET'])
@require_api_key
@require_token
def auth_invite_codes():
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    conn = get_db()
    rows = conn.execute(
        '''SELECT ic.*,
                  u.username as created_by_name,
                  u2.username as used_by_name
           FROM invite_codes ic
           LEFT JOIN users u ON ic.created_by = u.id
           LEFT JOIN users u2 ON ic.used_by = u2.id
           ORDER BY ic.created_at DESC'''
    ).fetchall()
    conn.close()
    return jsonify({'codes': [dict(r) for r in rows]})


# ---- 生成邀请码 ----
@app.route('/api/auth/generate_invite', methods=['POST'])
@require_api_key
@require_token
def auth_generate_invite():
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    note = data.get('note', '')
    code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:8].upper()
    conn = get_db()
    conn.execute(
        'INSERT INTO invite_codes (code, created_by, created_at, note) VALUES (?, ?, ?, ?)',
        (code, g.user_id, datetime.now().isoformat(), note)
    )
    conn.commit()
    conn.close()
    return jsonify({'code': code})


# ---- 用户列表 ----
@app.route('/api/auth/users', methods=['GET'])
@require_api_key
@require_token
def auth_users():
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT id, username, is_admin, is_active, created_at FROM users ORDER BY id'
    ).fetchall()
    conn.close()
    return jsonify({'users': [dict(r) for r in rows]})


# ---- 切换用户启用/停用 ----
@app.route('/api/auth/users/<int:uid>/toggle', methods=['POST'])
@require_api_key
@require_token
def auth_toggle_user(uid):
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    conn = get_db()
    row = conn.execute('SELECT is_active FROM users WHERE id = ?', (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
    new_state = 0 if row['is_active'] else 1
    conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_state, uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'is_active': bool(new_state)})


# ---- 删除用户 ----
@app.route('/api/auth/users/<int:uid>/delete', methods=['POST'])
@require_api_key
@require_token
def auth_delete_user(uid):
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    if uid == g.user_id:
        return jsonify({'error': '不能删除自己'}), 400
    conn = get_db()
    row = conn.execute('SELECT is_admin FROM users WHERE id = ?', (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
    if row['is_admin']:
        conn.close()
        return jsonify({'error': '不能删除管理员'}), 400
    conn.execute('DELETE FROM users WHERE id = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---- 重置密码 ----
@app.route('/api/auth/users/<int:uid>/reset_password', methods=['POST'])
@require_api_key
@require_token
def auth_reset_password(uid):
    if not g.is_admin:
        return jsonify({'error': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    new_pwd = data.get('password', '')
    if len(new_pwd) < 6:
        return jsonify({'error': '密码至少6位'}), 400
    conn = get_db()
    row = conn.execute('SELECT id FROM users WHERE id = ?', (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                 (_hash_password(new_pwd), uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ 启动 ============
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    print(f'认证服务器启动: http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('DEBUG', '0') == '1')
else:
    # 被 gunicorn 导入时自动初始化
    init_db()
