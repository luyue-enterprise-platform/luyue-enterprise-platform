# -*- coding: utf-8 -*-
"""
鲁岳企业服务·综合智能平台 — 自动更新助手
被 launcher.py 调用：下载新版本 EXE 并替换当前程序，然后重启。
"""
import os
import sys
import time
import shutil
import subprocess
import urllib.request
import urllib.error


def log(msg):
    try:
        with open(os.path.join(os.path.dirname(sys.executable), 'update.log'), 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass


def main():
    # 参数：当前 EXE 路径、新版本下载 URL、重启标志
    if len(sys.argv) < 3:
        log('参数不足，退出')
        return

    current_exe = sys.argv[1]
    download_url = sys.argv[2]
    restart = len(sys.argv) > 3 and sys.argv[3] == 'restart'

    base_dir = os.path.dirname(current_exe)
    temp_exe = os.path.join(base_dir, '鲁岳企业服务_综合智能平台.new.exe')
    backup_exe = os.path.join(base_dir, '鲁岳企业服务_综合智能平台.bak.exe')

    try:
        log(f'开始下载更新: {download_url}')
        # 下载新文件到临时位置
        headers = {'User-Agent': 'LuyueUpdater/1.0'}
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(temp_exe, 'wb') as f:
                shutil.copyfileobj(resp, f)
        log(f'下载完成: {temp_exe} ({os.path.getsize(temp_exe)} bytes)')
    except Exception as e:
        log(f'下载失败: {e}')
        if os.path.exists(temp_exe):
            os.remove(temp_exe)
        return

    # 等待原进程退出
    log('等待原进程退出...')
    time.sleep(3)

    try:
        # 备份当前 EXE
        if os.path.exists(backup_exe):
            os.remove(backup_exe)
        if os.path.exists(current_exe):
            shutil.move(current_exe, backup_exe)

        # 移动新 EXE 到正式位置
        shutil.move(temp_exe, current_exe)
        log('替换完成')

        if restart:
            log('启动新版本')
            subprocess.Popen([current_exe], cwd=base_dir, shell=False)
    except Exception as e:
        log(f'替换失败: {e}')
        # 尝试恢复备份
        if os.path.exists(backup_exe) and not os.path.exists(current_exe):
            shutil.move(backup_exe, current_exe)


if __name__ == '__main__':
    main()
