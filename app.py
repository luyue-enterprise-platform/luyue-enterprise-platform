# -*- coding: utf-8 -*-
"""
鲁岳企业服务·重点群体涉税申报项目资料处理综合智能平台
统一入口 — 整合社保批量统计 + 劳动合同整理两个子模块
"""
import os
import sys
import base64
import threading
import subprocess
import tempfile
import time

# ============ 路径设置 ============
from core.paths import data_dir, resource_dir

# 资源目录（_MEIPASS 或项目根目录）— 用于静态/模板文件
RESOURCE_DIR = resource_dir()
# 数据目录（自动处理 Program Files 权限问题：安装时重定向至 %APPDATA%）
DATA_DIR = data_dir()

# 确保资源目录在 Python 路径中（用于 import core.*、modules.*）
if RESOURCE_DIR not in sys.path:
    sys.path.insert(0, RESOURCE_DIR)

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

app.secret_key = 'luyue_platform_sk_2024_a8f3b2e1c9d7'
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
from modules.medical.blueprint import medical_bp

app.register_blueprint(insurance_bp)
app.register_blueprint(contract_bp)
app.register_blueprint(pdf2word_bp)
app.register_blueprint(medical_bp)
logger.info('子模块 Blueprint 注册完成')


# ============ 当前版本（用于远程更新检查） ============
def _load_local_version():
    """读取本地的 version.json"""
    import json as _json
    candidates = [
        os.path.join(DATA_DIR, 'version.json'),
        os.path.join(RESOURCE_DIR, 'version.json'),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return _json.load(f)
            except Exception:
                pass
    return {'version': '1.0.0', 'version_code': 100, 'download_url': '', 'changelog': ''}

APP_VERSION = _load_local_version()
logger.info(f'当前版本: v{APP_VERSION.get("version", "?")} (code={APP_VERSION.get("version_code", "?")})')


# ============ 共享路由：登录 / 注册 / 退出 ============
from flask import request, jsonify, render_template, session, redirect, url_for
from core.auth import (
    create_user, verify_user, get_user_count,
    validate_invite_code, consume_invite_code,
    login_required, admin_required, get_remote_token,
    is_first_registration,
)

# ============ 记住密码：后端文件存储（localStorage 的可靠备份） ============
# WebView2 的 localStorage 磁盘持久化是异步的，应用关闭时可能来不及 flush
# 导致记住密码间歇性失效。此 API 通过同步文件 I/O 提供可靠备份。
_REMEMBER_FILE = os.path.join(DATA_DIR, 'data', 'remembered_login.json')


@app.route('/api/remember_login', methods=['GET'])
def api_get_remembered_login():
    """读取已保存的登录凭据（无需登录即可访问）"""
    import json as _json
    try:
        if os.path.isfile(_REMEMBER_FILE):
            with open(_REMEMBER_FILE, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            return jsonify({'ok': True, 'username': data.get('username', ''),
                            'password': data.get('password', '')})
    except Exception as e:
        logger.warning(f'读取记住密码文件失败: {e}')
    return jsonify({'ok': True, 'username': '', 'password': ''})


@app.route('/api/remember_login', methods=['POST'])
def api_save_remembered_login():
    """保存或清除登录凭据（无需登录即可访问）"""
    import json as _json
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    try:
        os.makedirs(os.path.dirname(_REMEMBER_FILE), exist_ok=True)
        payload = {'username': username, 'password': password}
        with open(_REMEMBER_FILE, 'w', encoding='utf-8') as f:
            _json.dump(payload, f, ensure_ascii=False)
        return jsonify({'ok': True})
    except Exception as e:
        logger.warning(f'保存记住密码文件失败: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500

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
            # 透传 verify_user 返回的真实错误（密码错/停用/认证服务不可用），
            # 避免把所有失败都硬编码成"用户名或密码错误"误导用户
            msg = err or '用户名或密码错误'
            if request.is_json:
                return jsonify({'error': msg}), 401
            return render_template('login.html', error=msg)

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    user_count = get_user_count()
    is_first = is_first_registration()

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
                                  is_first=is_first, error=err)
        if len(password) < 6:
            err = '密码至少6位'
            if request.is_json:
                return jsonify({'error': err}), 400
            return render_template('register.html', user_count=user_count,
                                  is_first=is_first, error=err)
        if password != confirm_password:
            err = '两次输入的密码不一致'
            if request.is_json:
                return jsonify({'error': err}), 400
            return render_template('register.html', user_count=user_count,
                                  is_first=is_first, error=err)

        # 判断是否首次注册（远程可达且count==0才算首次，远程不可达则要求邀请码）

        if is_first:
            # 首次注册：不需要邀请码，注册为管理员
            uid, err2 = create_user(username, password, is_admin=True)
            if uid is None:
                err_msg = err2 or '用户名已存在'
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
            # 非首次注册：必须输入邀请码
            invite_code = data.get('invite_code', '').strip()
            if not invite_code:
                err = '请输入邀请码'
                if request.is_json:
                    return jsonify({'error': err}), 400
                return render_template('register.html', user_count=user_count,
                                      is_first=False, error=err)
            if not validate_invite_code(invite_code):
                err = '邀请码无效或已被使用'
                if request.is_json:
                    return jsonify({'error': err}), 400
                return render_template('register.html', user_count=user_count,
                                      is_first=False, error=err)
            uid2, err3 = create_user(username, password,
                                     is_admin=False, invite_code=invite_code)
            if uid2 is None:
                err_msg2 = err3 or '用户名已存在'
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
                          is_first=is_first_registration())


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
        is_admin=session.get('is_admin', False),
        app_version=APP_VERSION.get('version', '1.0.0'),
        version_code=APP_VERSION.get('version_code', 100))


# ============ 版本检查 API ============
@app.route('/api/app/version', methods=['GET'])
@login_required
def api_app_version():
    """返回本地版本信息"""
    return jsonify({
        'ok': True,
        'version': APP_VERSION.get('version', '1.0.0'),
        'version_code': APP_VERSION.get('version_code', 100),
        'changelog': APP_VERSION.get('changelog', ''),
    })


# 远程版本信息地址（COS 首位：国内直连快；GitHub 兜底：COS 异常时可回退）
# 注意：COS 与 raw.githubusercontent 返回明文 JSON；GitHub Contents API 返回 base64（下方单独解码）
REMOTE_VERSION_URLS = [
    'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/version.json',
    'https://raw.githubusercontent.com/luyue-enterprise-platform/luyue-enterprise-platform/main/version.json',
    'https://api.github.com/repos/luyue-enterprise-platform/luyue-enterprise-platform/contents/version.json',
]


@app.route('/api/app/check_update', methods=['GET'])
def api_check_update():
    """从远程抓取最新版本信息，对比 version_code 判断是否需要更新

    免登录：登录页也需要显示升级提示（认证服务异常导致无法登录时，
    用户必须在登录前就能看到新版本并下载修复版本）。
    接口仅返回版本/更新说明/公开下载地址，无敏感数据。
    """
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue

    last_err = None
    for url in REMOTE_VERSION_URLS:
        try:
            req = _ur.Request(url, headers={'User-Agent': 'LuyueApp/1.1.5'})
            with _ur.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                # GitHub Contents API 返回 JSON 带 content 字段（base64）
                if 'api.github.com/repos' in url and '"content"' in raw:
                    api_data = _json.loads(raw)
                    raw = base64.b64decode(api_data['content']).decode('utf-8')
                data = _json.loads(raw)
                local_code = APP_VERSION.get('version_code', 100)
                remote_code = int(data.get('version_code', 0) or 0)
                return jsonify({
                    'ok': True,
                    'has_update': remote_code > local_code,
                    'local_version': APP_VERSION.get('version', '1.0.0'),
                    'local_code': local_code,
                    'remote_version': data.get('version', ''),
                    'remote_code': remote_code,
                    'download_url': data.get('download_url', ''),
                    'changelog': data.get('changelog', ''),
                    'mandatory': bool(data.get('mandatory', False)),
                })
        except _ue.HTTPError as e:
            last_err = f'HTTP {e.code}'
            continue
        except Exception as e:
            last_err = str(e)
            continue

    return jsonify({
        'ok': False,
        'error': f'检查更新失败: {last_err or "网络异常"}',
        'has_update': False,
    }), 200


# ============ 自动升级（下载完成后静默安装） ============
_update_lock = threading.Lock()
_update_state = {
    'status': 'idle',   # idle / downloading / installing / error
    'percent': 0,
    'downloaded': 0,
    'total': 0,
    'version': '',
    'error': '',
}

# 允许自动下载的安装包地址白名单（防止接口被滥用去下载任意文件）
_UPDATE_ALLOWED_PREFIXES = (
    'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/',
    'https://github.com/luyue-enterprise-platform/',
)
# 安装包最小体积（防错误页/截断文件被当成安装包执行）
_UPDATE_MIN_SIZE = 5 * 1024 * 1024


def _do_update(download_url, version):
    """后台线程：下载安装包 → 校验 → 静默安装 → 退出当前进程释放 EXE 锁"""
    import urllib.request as _ur
    tmp_path = None
    try:
        req = _ur.Request(download_url, headers={'User-Agent': 'LuyueApp/1.1.5'})
        with _ur.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            fd, tmp_path = tempfile.mkstemp(prefix='ly_update_', suffix='.exe')
            with os.fdopen(fd, 'wb') as f:
                downloaded = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with _update_lock:
                        _update_state['downloaded'] = downloaded
                        _update_state['total'] = total
                        if total > 0:
                            _update_state['percent'] = min(99, int(downloaded * 100 / total))
        # 校验：体积合理 + PE 头 + 与 Content-Length 一致（防错误页/截断）
        fsize = os.path.getsize(tmp_path)
        with open(tmp_path, 'rb') as f:
            head = f.read(2)
        if fsize < _UPDATE_MIN_SIZE or head != b'MZ' or (total and fsize != total):
            raise RuntimeError('安装包校验失败（文件不完整），请重试或改用浏览器下载')
        with _update_lock:
            _update_state['status'] = 'installing'
            _update_state['percent'] = 100
        logger.info(f'升级包下载完成({fsize}字节)，开始静默安装: {tmp_path}')
        subprocess.Popen([
            tmp_path, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'
        ], close_fds=True)
        # 给安装器启动留出时间后退出当前进程，释放 EXE 文件锁（安装器负责重启程序）
        time.sleep(5)
        os._exit(0)
    except Exception as e:
        logger.error(f'自动升级失败: {e}')
        with _update_lock:
            _update_state['status'] = 'error'
            _update_state['error'] = str(e)
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.route('/api/app/start_update', methods=['POST'])
def api_start_update():
    """启动自动升级：后台下载安装包，完成后静默安装并自动重启

    免登录（登录页救火场景也要能用）；仅允许白名单内的下载地址，防滥用。
    """
    data = request.get_json(silent=True) or {}
    url = (data.get('download_url') or '').strip()
    version = (data.get('version') or '').strip()
    if not url or not url.startswith(_UPDATE_ALLOWED_PREFIXES) or not url.lower().endswith('.exe'):
        return jsonify({'ok': False, 'error': '下载地址不合法或未在白名单内'}), 400
    with _update_lock:
        if _update_state['status'] in ('downloading', 'installing'):
            return jsonify({'ok': False, 'error': '已有升级任务进行中',
                            'status': _update_state['status']}), 409
        _update_state.update({
            'status': 'downloading', 'percent': 0,
            'downloaded': 0, 'total': 0,
            'version': version, 'error': '',
        })
    t = threading.Thread(target=_do_update, args=(url, version), daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/api/app/update_progress', methods=['GET'])
def api_update_progress():
    """查询自动升级进度（免登录）"""
    with _update_lock:
        return jsonify({'ok': True, **_update_state})


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
    data = request.get_json(silent=True) or {}
    note = data.get('note', '')
    count = min(int(data.get('count', 1)), 100)
    codes = []
    for _ in range(count):
        code = generate_invite_code(session.get('user_id'), note)
        if code:
            codes.append(code)
    if not codes:
        return jsonify({'error': '生成邀请码失败，请检查认证服务是否正常'}), 500
    # 向后兼容：单个邀请码时同时返回 code 和 codes
    result = {'codes': codes}
    if len(codes) == 1:
        result['code'] = codes[0]
    return jsonify(result)


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
