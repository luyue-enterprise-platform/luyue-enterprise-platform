# -*- coding: utf-8 -*-
"""
鲁岳企业服务·重点群体涉税申报项目资料处理综合智能平台 — 桌面启动器
使用原生窗口（WebView2），无需外部浏览器
"""
import os
import sys
import glob
import ctypes
import tempfile
import threading
import time
import socket
import traceback
import logging

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


def _write_crash_log(error_msg):
    """将崩溃信息写入日志文件，方便排查问题"""
    try:
        log_dir = os.path.join(BASE_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'crash.log')
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f'\n{"="*60}\n')
            f.write(f'[{timestamp}] 程序启动失败\n')
            f.write(f'{"="*60}\n')
            f.write(error_msg)
            f.write('\n')
    except Exception:
        pass


def _show_error_box(title, message):
    """显示 Windows 原生错误对话框（不依赖 WebView2）"""
    try:
        import ctypes
        # MB_OK | MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        # 如果连 MessageBox 都失败了，打印到 stderr（开发模式下可见）
        print(f'[ERROR] {title}: {message}', file=sys.stderr)


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


def _check_webview2_runtime():
    """
    检查 WebView2 Runtime 是否已安装。
    返回: (True, '') 或 (False, error_message)
    """
    try:
        import winreg
        # WebView2 Runtime 的注册表位置
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
        ]
        for hive, path in reg_paths:
            try:
                key = winreg.OpenKey(hive, path)
                winreg.CloseKey(key)
                return True, ''
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return False, '未检测到 WebView2 Runtime。请安装 WebView2 Runtime 后重试。\n\n可从以下地址下载：\nhttps://developer.microsoft.com/microsoft-edge/webview2/'
    except Exception as e:
        # 注册表检查失败，不阻止启动（让 pywebview 自行处理）
        return True, ''


def main():
    # v1.1.56 单实例守护：检测到已有实例在运行则直接退出。
    # 为什么必要：自动升级要求"升级方退出 → 释放主 EXE 文件锁 → 安装器覆盖"。
    # 若用户重复双击启动第二个实例（launcher 端口顺延会让它跑起来），两个进程
    # 同时锁定主 EXE，升级时安装器永远无法覆盖（实测双实例下升级失败）。单实例
    # 守护同时避免双窗口/双端口困惑。互斥量句柄随进程退出由内核自动释放，不会
    # 残留陈旧锁。
    # 注意：此互斥量只作单实例守护，installer.iss 绝不引用它（不配 AppMutex）
    # 且显式 CloseApplications=no——AppMutex 让静默安装器检测到升级方仍存活即
    # 取消(rc=1)；CloseApplications 在 Inno 6 默认开启，其 Restart Manager 关
    # 不掉运行中进程即静默 Abort(rc=5)，两者都会阻断无人值守升级。升级靠
    # app.py 2.5s 内自行退出释放 EXE 锁完成（v1.1.56 修订教训，详见 installer.iss
    # 与 _do_update 注释）。
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, 'LuyuePlatform_SingletonMutex')
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            _show_error_box('程序已在运行', '鲁岳企业服务·综合智能平台 已在运行。\n请勿重复启动。')
            return
    except Exception:
        pass

    # v1.1.56：清理历史自动升级残留的临时安装包（%TEMP%\ly_update_*.exe，
    # 每个约 324MB）。此刻本进程是新版本，说明上一轮安装器已结束，残留可安全删除；
    # 只删 10 分钟前创建的，避免误删另一实例正在下载/安装中的升级包。
    try:
        _stale_threshold = time.time() - 600
        for _name in glob.glob(os.path.join(tempfile.gettempdir(), 'ly_update_*.exe')):
            try:
                if os.path.getmtime(_name) < _stale_threshold:
                    os.remove(_name)
            except Exception:
                pass
    except Exception:
        pass

    # 确保必要目录存在
    for d in ['data', 'uploads', 'outputs', 'logs']:
        os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

    # 确定可写数据目录（处理 Program Files 权限重定向）
    from core.paths import data_dir as get_data_dir
    DATA_DIR = get_data_dir()

    # WebView2 用户数据目录（固定路径，确保 localStorage 持久化）
    webview_data_dir = os.path.join(DATA_DIR, 'data', 'webview_data')
    os.makedirs(webview_data_dir, exist_ok=True)

    # 查找可用端口
    global PORT
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
    webview.start(gui='edgechromium', debug=False, private_mode=False,
                  storage_path=webview_data_dir)

    # 等待 WebView2 完成 localStorage 磁盘持久化（避免进程退出时数据丢失）
    time.sleep(0.5)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        error_detail = traceback.format_exc()
        _write_crash_log(error_detail)

        # 根据错误类型给出更友好的提示
        error_str = str(e).lower()
        if 'webview2' in error_str or 'edge' in error_str or 'corewebview' in error_str:
            _show_error_box(
                '启动失败 — WebView2 运行时缺失',
                '程序启动失败：未检测到 WebView2 运行时组件。\n\n'
                '请安装 Microsoft Edge WebView2 Runtime 后重试。\n\n'
                '下载地址：\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n\n'
                f'错误详情：\n{e}'
            )
        elif 'dll' in error_str or 'module' in error_str or 'import' in error_str:
            _show_error_box(
                '启动失败 — 缺少依赖组件',
                f'程序启动失败：缺少必要的系统组件或依赖库。\n\n'
                f'错误详情：\n{e}\n\n'
                f'请联系统件供应商或技术支持。'
            )
        else:
            _show_error_box(
                '启动失败',
                f'程序启动遇到错误：\n\n{e}\n\n'
                f'详细日志已保存到：{os.path.join(BASE_DIR, "logs", "crash.log")}'
            )
