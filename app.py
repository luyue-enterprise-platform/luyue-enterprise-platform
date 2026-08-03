# -*- coding: utf-8 -*-
"""
鲁岳企业服务·重点群体涉税申报项目资料处理综合智能平台
统一入口 — 整合社保批量统计 + 劳动合同整理两个子模块
"""
import os
import sys

# ============ 路径设置 ============
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    RESOURCE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RESOURCE_DIR

# 确保项目根目录在 Python 路径中
if DATA_DIR not in sys.path:
    sys.path.insert(0, DATA_DIR)

# ============ 日志配置 ============
import logging
LOG_DIR = os.path.join(DATA_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'app.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('platform')

# ============ 创建 Flask 应用 ============
from flask import Flask

app = Flask(__name__,
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static'))

app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# 添加门户模板目录到 Jinja2 搜索路径
import jinja2
portal_tpl = os.path.join(RESOURCE_DIR, 'portal', 'templates')
if os.path.isdir(portal_tpl):
    my_loader = jinja2.ChoiceLoader([
        jinja2.FileSystemLoader(portal_tpl),
        app.jinja_loader,
    ])
    app.jinja_loader = my_loader

# ============ 初始化共享数据库 ============
from core.auth import init_db
init_db(data_dir=os.path.join(DATA_DIR, 'data'))
logger.info('共享数据库初始化完成')

# ============ 注册子模块 Blueprint ============
from modules.insurance.blueprint import insurance_bp
from modules.contract.blueprint import contract_bp
from modules.pdf2word.blueprint import pdf2word_bp

app.register_blueprint(insurance_bp)
app.register_blueprint(contract_bp)
app.register_blueprint(pdf2word_bp)
logger.info('子模块 Blueprint 注册完成')

# ============ 共享路由：登录 / 注册 / 退出 ============
from flask import request, jsonify, render_template, session, redirect, url_for
from core.auth import (
    create_user, verify_user, get_user_count,
    validate_invite_code, consume_invite_code,
    login_required, admin_required, get_remote_token
)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 兼容 HTML 表单 POST 和 AJAX JSON POST
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form

        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            if request.is_json:
                return jsonify({'error': '请输入用户名和密码'}), 400
            return render_template('login.html', error='请输入用户名和密码')

        user, err = verify_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            # 远程模式：存储 API token 到 session
            remote_token = user.get('_token') or get_remote_token()
            if remote_token:
                session['_auth_token'] = remote_token
            if request.is_json:
                return jsonify({'ok': True})
            return redirect('/')
        else:
            if request.is_json:
                return jsonify({'error': '用户名或密码错误'}), 401
            return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    user_count = get_user_count()

    if request.method == 'POST':
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form

        username = data.get('username', '').strip()
        password = data.get('password', '')
        confirm_password = data.get('confirm_password', '')

        if len(username) < 2:
            err = '用户名至少2位'
            if request.is_json:
                return jsonify({'error': err}), 400
            return render_template('register.html', user_count=user_count,
                                  is_first=(user_count == 0), error=err)
        if len(password) < 6:
            err = '密码至少6位'
            if request.is_json:
                return jsonify({'error': err}), 400
            return render_template('register.html', user_count=user_count,
                                  is_first=(user_count == 0), error=err)
        if password != confirm_password:
            err = '两次输入的密码不一致'
            if request.is_json:
                return jsonify({'error': err}), 400
            return render_template('register.html', user_count=user_count,
                                  is_first=(user_count == 0), error=err)

        if user_count == 0:
            uid, err = create_user(username, password, is_admin=True)
            if uid is None:
                err_msg = err or '用户名已存在'
                if request.is_json:
                    return jsonify({'error': err_msg}), 400
                return render_template('register.html', user_count=user_count,
                                      is_first=True, error=err_msg)
            session['user_id'] = uid
            session['username'] = username
            session['is_admin'] = True
            # 远程模式：存储 API token 到 session
            remote_token = get_remote_token()
            if remote_token:
                session['_auth_token'] = remote_token
            if request.is_json:
                return jsonify({'ok': True})
            return redirect('/')
        else:
            invite_code = data.get('invite_code', '').strip()
            if not validate_invite_code(invite_code):
                err = '邀请码无效或已被使用'
                if request.is_json:
                    return jsonify({'error': err}), 400
                return render_template('register.html', user_count=user_count,
                                      is_first=False, error=err)
            # 远程模式下将邀请码传给 create_user
            uid2, err2 = create_user(username, password, invite_code=invite_code)
            if uid2 is None:
                err_msg2 = err2 or '用户名已存在'
                if request.is_json:
                    return jsonify({'error': err_msg2}), 400
                return render_template('register.html', user_count=user_count,
                                      is_first=False, error=err_msg2)
            consume_invite_code(invite_code, uid2)
            session['user_id'] = uid2
            session['username'] = username
            session['is_admin'] = False
            # 远程模式：存储 API token 到 session
            remote_token2 = get_remote_token()
            if remote_token2:
                session['_auth_token'] = remote_token2
            if request.is_json:
                return jsonify({'ok': True})
            return redirect('/')

    return render_template('register.html', user_count=user_count,
                          is_first=(user_count == 0))


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ============ 门户首页 ============
@app.route('/')
@login_required
def portal():
    return render_template('portal.html',
        username=session.get('username', ''),
        is_admin=session.get('is_admin', False))


# ============ 修改密码 API（门户 + 子模块共用） ============
@app.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    from core.auth import verify_user, reset_password, _is_remote
    data = request.get_json(silent=True) or {}
    old_pwd = data.get('old_password', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': '新密码至少6位'}), 400

    if _is_remote():
        # 远程模式：通过 API 修改密码
        import urllib.request, urllib.error, json as _json
        from core.auth import AUTH_SERVER_URL, AUTH_API_KEY
        token = session.get('_auth_token', '')
        url = AUTH_SERVER_URL.rstrip('/') + '/api/auth/change_password'
        headers = {
            'X-API-Key': AUTH_API_KEY,
            'X-User-Token': token,
            'Content-Type': 'application/json',
        }
        body = _json.dumps({'old_password': old_pwd, 'new_password': new_pwd}).encode('utf-8')
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read().decode('utf-8'))
                if resp.status == 200:
                    return jsonify({'ok': True})
                return jsonify({'error': result.get('error', '修改失败')}), resp.status
        except Exception as e:
            return jsonify({'error': f'修改失败: {e}'}), 500

    username = session.get('username', '')
    user, err = verify_user(username, old_pwd)
    if not user:
        return jsonify({'error': '旧密码错误'}), 400

    reset_password(user['id'], new_pwd)
    return jsonify({'ok': True})


# ============ 邀请码管理（管理员） ============
@app.route('/api/invite_codes')
@admin_required
def api_invite_codes():
    from core.auth import get_invite_codes
    codes = get_invite_codes()
    return jsonify({'codes': codes})


@app.route('/api/generate_invite', methods=['POST'])
@admin_required
def api_generate_invite():
    from core.auth import generate_invite_code
    note = (request.get_json(silent=True) or {}).get('note', '')
    code = generate_invite_code(session.get('user_id'), note)
    return jsonify({'code': code})


# ============ 用户管理（管理员） ============
@app.route('/api/users')
@admin_required
def api_users():
    from core.auth import get_all_users
    users = get_all_users()
    return jsonify({'users': users})


@app.route('/api/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def api_toggle_user(uid):
    from core.auth import toggle_user_active, get_user_by_id
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    toggle_user_active(uid)
    return jsonify({'ok': True})


@app.route('/api/users/<int:uid>/delete', methods=['POST'])
@admin_required
def api_delete_user(uid):
    from core.auth import delete_user, get_user_by_id
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if uid == session.get('user_id'):
        return jsonify({'error': '不能删除自己'}), 400
    if user.get('is_admin'):
        return jsonify({'error': '不能删除管理员'}), 400
    delete_user(uid)
    return jsonify({'ok': True})


@app.route('/api/users/<int:uid>/reset_password', methods=['POST'])
@admin_required
def api_reset_password(uid):
    from core.auth import reset_password, get_user_by_id
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    data = request.get_json(silent=True) or {}
    new_pwd = data.get('password', '')
    if len(new_pwd) < 6:
        return jsonify({'error': '密码至少6位'}), 400
    reset_password(uid, new_pwd)
    return jsonify({'ok': True})


# ============ 启动入口 ============
def start_server(port=5000):
    logger.info(f'综合智能平台启动: http://127.0.0.1:{port}')
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 5000))
    start_server(port)
