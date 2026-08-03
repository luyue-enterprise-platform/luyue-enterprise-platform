# -*- coding: utf-8 -*-
r"""
路径处理模块

处理 Windows 系统的 C:\Program Files\ 等系统保护目录的文件写入问题。
当 EXE 安装在 Program Files 时，用户需要写入的文件放在 %APPDATA%\鲁岳企业服务\ 目录。

使用约定：
- 配置文件 / 缓存 -> EXE 目录（如果可写）或 %APPDATA%\鲁岳企业服务\
- 程序文件（templates 等）-> EXE 所在目录
"""
import os
import sys

APP_NAME = '鲁岳企业服务'


def _is_protected_dir(path: str) -> bool:
    """检查路径是否在 Windows 系统 Program Files 目录"""
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
    """获取应用的数据目录

    Returns:
        可写入的目录路径。安装时重定向，开发时返回项目目录。
    """
    if getattr(sys, 'frozen', False):
        # 打包模式：EXE
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if _is_protected_dir(exe_dir):
            # 系统保护目录（Program Files）-> 重定向至 %APPDATA%
            appdata = os.environ.get('APPDATA')
            if appdata:
                user_dir = os.path.join(appdata, APP_NAME)
                os.makedirs(user_dir, exist_ok=True)
                return user_dir
        # 非保护目录（EXE 在 D:\ 或 F:\ 等）-> 直接使用 EXE 目录
        return exe_dir

    # 开发模式：使用项目根目录（同 core/ 的上级目录）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return project_root


def get_resource_dir() -> str:
    """获取资源目录（templates、static、modules 等）

    Returns:
        打包时返回 _MEIPASS 目录。开发时返回项目根目录。
    """
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 缓存机制（避免多次 IO 和路径计算）
_DATA_DIR_CACHE: str = None
_RESOURCE_DIR_CACHE: str = None


def data_dir() -> str:
    """get_data_dir 的缓存版本，用于多次调用"""
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
