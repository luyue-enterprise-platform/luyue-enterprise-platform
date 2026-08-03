# -*- coding: utf-8 -*-
"""
跨平台数据目录解析工具

解决 Windows 安装到 C:\\Program Files\\ 等受保护目录时的写入权限问题。
当 EXE 被安装到 Program Files 下时，普通用户无法向该目录写入，
需要将数据重定向到 %APPDATA%\\鲁岳企业服务\\。

约定：
- 源码运行 / 便携模式：使用 EXE（或脚本）所在目录
- 安装到 Program Files 下：自动重定向到 %APPDATA%
"""
import os
import sys

APP_NAME = '鲁岳企业服务'


def _is_protected_dir(path: str) -> bool:
    """检查路径是否在受 Windows 保护的 Program Files 目录下"""
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


def get_data_dir() -> str:
    """获取可写的用户数据目录

    Returns:
        目录路径字符串。已确保该目录存在。
    """
    if getattr(sys, 'frozen', False):
        # 打包后运行：EXE 所在目录
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if _is_protected_dir(exe_dir):
            # 受保护目录（Program Files）→ 重定向到 %APPDATA%
            appdata = os.environ.get('APPDATA')
            if appdata:
                user_dir = os.path.join(appdata, APP_NAME)
                os.makedirs(user_dir, exist_ok=True)
                return user_dir
        # 便携模式（EXE 在 D:\\、E:\\ 等非保护位置）→ 仍用 EXE 目录
        return exe_dir

    # 源码运行：使用项目根目录（即 core/ 目录的父目录）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return project_root


def get_resource_dir() -> str:
    """获取只读资源目录（templates、static、modules 等）

    Returns:
        资源目录路径。打包运行时为 _MEIPASS 临时目录，源码运行时为项目根目录。
    """
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 模块级缓存（避免重复 IO 与目录判断）
_DATA_DIR_CACHE: str = None
_RESOURCE_DIR_CACHE: str = None


def data_dir() -> str:
    """get_data_dir 的缓存版本，便于重复使用"""
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE is None:
        _DATA_DIR_CACHE = get_data_dir()
    return _DATA_DIR_CACHE


def resource_dir() -> str:
    """get_resource_dir 的缓存版本"""
    global _RESOURCE_DIR_CACHE
    if _RESOURCE_DIR_CACHE is None:
        _RESOURCE_DIR_CACHE = get_resource_dir()
    return _RESOURCE_DIR_CACHE
