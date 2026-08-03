# -*- coding: utf-8 -*-
"""
鲁岳企业服务·综合智能平台 — 自动更新助手
被 launcher.py 调用：下载新版本 EXE 并替换当前程序，然后重启。

注意：当 EXE 安装在 C:\\Program Files\\ 等受保护目录时，
无法直接替换原 EXE（需要管理员权限），此时会回退到
提示用户手动下载新版本。
"""
import os
import sys
import time
import shutil
import subprocess
import urllib.request
import urllib.error


# ============ 路径工具：处理 Program Files 受保护目录 ============
import tempfile


def _is_protected_dir(path: str) -> bool:
    """检查路径是否在 Windows 保护的 Program Files 目录下"""
    if sys.platform != 'win32' or not path:
        return False
    try:
        npath = os.path.normcase(os.path.abspath(path))
        candidates = [
            os.path.normcase(os.path.abspath(os.environ.get('ProgramFiles', r'C:\Program Files'))),
            os.path.normcase(os.path.abspath(os.environ.get('ProgramW6432', r'C:\Program Files'))),
            os.path.normcase(os.path.abspath(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))),
        ]
        for base in candidates:
            if base and npath.startswith(base + os.sep):
                return True
        return False
    except Exception:
        return False


def _get_work_dir(base_dir: str) -> str:
    """获取可写工作目录：受保护目录 → 重定向到 %TEMP%/鲁岳企业服务/updater"""
    if _is_protected_dir(base_dir):
        work = os.path.join(tempfile.gettempdir(), '鲁岳企业服务', 'updater')
        os.makedirs(work, exist_ok=True)
        return work
    return base_dir


# ============ 日志 ============

def log(msg):
    try:
        # 始终写到 %TEMP% 以避免受保护目录权限问题
        log_dir = os.path.join(tempfile.gettempdir(), '鲁岳企业服务', 'updater')
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, 'update.log'), 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass


# ============ 主流程 ============

def main():
    # 参数：当前 EXE 路径、新版本下载 URL、重启标志
    if len(sys.argv) < 3:
        log('参数不足，退出')
        return

    current_exe = sys.argv[1]
    download_url = sys.argv[2]
    restart = len(sys.argv) > 3 and sys.argv[3] == 'restart'

    base_dir = os.path.dirname(current_exe)
    protected = _is_protected_dir(base_dir)
    work_dir = _get_work_dir(base_dir)

    log(f'当前 EXE 目录: {base_dir}, 受保护={protected}')
    log(f'工作目录: {work_dir}')

    # 受保护目录下的临时文件名（避免与其他进程冲突）
    temp_exe = os.path.join(work_dir, f'鲁岳企业服务_综合智能平台_{int(time.time())}.new.exe')
    backup_exe = os.path.join(work_dir, f'鲁岳企业服务_综合智能平台.bak.exe')

    try:
        log(f'开始下载更新: {download_url}')
        headers = {'User-Agent': 'LuyueUpdater/1.0'}
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(temp_exe, 'wb') as f:
                shutil.copyfileobj(resp, f)
        log(f'下载完成: {temp_exe} ({os.path.getsize(temp_exe)} bytes)')
    except Exception as e:
        log(f'下载失败: {e}')
        if os.path.exists(temp_exe):
            try:
                os.remove(temp_exe)
            except Exception:
                pass
        return

    # 等待原进程退出
    log('等待原进程退出...')
    time.sleep(3)

    # 如果是受保护目录：无法自动替换 → 提示用户手动操作
    if protected:
        log(f'原 EXE 位于受保护目录 {base_dir}，无法自动替换')
        log(f'请手动将下载的新版本安装到 {base_dir}（覆盖现有文件），或重新安装到用户目录')
        # 将下载好的新版本保留在 %TEMP% 供用户手动处理
        final_path = os.path.join(work_dir, '鲁岳企业服务_综合智能平台_新版本.exe')
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            shutil.copy2(temp_exe, final_path)
            log(f'新版本已保存到: {final_path}')
            # 用默认应用打开所在文件夹
            try:
                subprocess.Popen(['explorer', '/select,', final_path])
            except Exception:
                pass
        except Exception as e:
            log(f'保存新版本失败: {e}')
        return

    # 正常情况：替换原 EXE
    try:
        if os.path.exists(backup_exe):
            try:
                os.remove(backup_exe)
            except Exception:
                pass
        if os.path.exists(current_exe):
            shutil.move(current_exe, backup_exe)

        shutil.move(temp_exe, current_exe)
        log('替换完成')

        if restart:
            log('启动新版本')
            subprocess.Popen([current_exe], cwd=base_dir, shell=False)
    except Exception as e:
        log(f'替换失败: {e}')
        # 尝试恢复备份
        if os.path.exists(backup_exe) and not os.path.exists(current_exe):
            try:
                shutil.move(backup_exe, current_exe)
            except Exception:
                pass


if __name__ == '__main__':
    main()
