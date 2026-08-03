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

# 获取应用根目录
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    # 单文件 exe 模式：PyInstaller 把二进制解压到 sys._MEIPASS 临时目录
    # 显式把 onnxruntime/capi 加入 DLL 搜索路径，避免打包后 DLL load failed
    try:
        meipass = sys._MEIPASS
        onnx_capi_dir = os.path.join(meipass, 'onnxruntime', 'capi')
        if os.path.isdir(onnx_capi_dir) and hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(onnx_capi_dir)
    except Exception:
        pass
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(BASE_DIR)

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

    # 确保必要目录存在
    for d in ['data', 'uploads', 'outputs', 'logs']:
        os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

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
