# -*- coding: utf-8 -*-
"""
鲁岳企业服务·重点群体涉税申报项目资料处理综合智能平台 — 桌面启动器
使用原生窗口（WebView2），无需外部浏览器
"""
import os
import sys
import threading
import time
import socket

# 添加项目根目录到 Python 路径（打包时 core/ 在 _MEIPASS 内）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else sys._MEIPASS)

# 共享路径工具：自动处理 Program Files 受保护目录
from core.paths import data_dir, resource_dir

# 资源目录（模板、静态文件、core 模块等）
RESOURCE_DIR = resource_dir()
if RESOURCE_DIR not in sys.path:
    sys.path.insert(0, RESOURCE_DIR)

# 显式把 onnxruntime/capi 加入 DLL 搜索路径，避免打包后 DLL load failed
if getattr(sys, 'frozen', False):
    try:
        meipass = sys._MEIPASS
        onnx_capi_dir = os.path.join(meipass, 'onnxruntime', 'capi')
        if os.path.isdir(onnx_capi_dir) and hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(onnx_capi_dir)
    except Exception:
        pass

# 数据目录（可写）：自动重定向到 %APPDATA% 解决 Program Files 权限问题
DATA_DIR = data_dir()
os.chdir(DATA_DIR)

HOST = '127.0.0.1'
PORT = 5000
WINDOW_TITLE = '鲁岳企业服务·综合智能平台'


def is_port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def find_free_port(start_port=5000, max_attempts=100):
    port = start_port
    for _ in range(max_attempts):
        if not is_port_in_use(HOST, port):
            return port
        port += 1
    return start_port


def main():
    global PORT

    # 确保必要目录存在（数据目录：data/uploads/outputs/logs）
    for d in ['data', 'uploads', 'outputs', 'logs']:
        os.makedirs(os.path.join(DATA_DIR, d), exist_ok=True)

    # 查找可用端口
    if is_port_in_use(HOST, PORT):
        PORT = find_free_port(PORT)

    os.environ['FLASK_PORT'] = str(PORT)

    # 后台启动 Flask
    from app import app as flask_app

    def run_flask():
        flask_app.run(
            host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False
        )

    server_thread = threading.Thread(target=run_flask, daemon=True)
    server_thread.start()

    # 等待 Flask 就绪
    for _ in range(50):
        if is_port_in_use(HOST, PORT):
            break
        time.sleep(0.2)

    url = f'http://{HOST}:{PORT}'

    # 创建原生桌面窗口（内嵌 Edge WebView2）
    import webview
    window = webview.create_window(
        title=WINDOW_TITLE,
        url=url,
        width=1080,
        height=720,
        min_size=(800, 540),
        resizable=True,
        confirm_close=True,
    )
    webview.start(gui='edgechromium', debug=False)


if __name__ == '__main__':
    main()
