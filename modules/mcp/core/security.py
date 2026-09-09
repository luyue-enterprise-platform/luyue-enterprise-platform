# -*- coding: utf-8 -*-
"""MCP 访问令牌与总开关

配置持久化在**数据目录**（core.paths.data_dir()）下的 data/mcp_config.json：
- token:    Bearer 令牌，首次访问时自动生成；可在门户页查看或重新生成
- enabled:  总开关，false 时 /mcp 端点直接拒绝服务（业务不受影响）

安全边界：Flask 已绑定 127.0.0.1，外部网络不可达；令牌用于防止本机上
其他进程/用户在未授权情况下驱动本平台的业务能力。
"""
import json
import os
import secrets
import threading

from core.paths import data_dir

_LOCK = threading.Lock()
_CONFIG_PATH = None

_DEFAULTS = {'enabled': True, 'token': None}


def _config_path():
    global _CONFIG_PATH
    if _CONFIG_PATH is None:
        d = os.path.join(data_dir(), 'data')
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        _CONFIG_PATH = os.path.join(d, 'mcp_config.json')
    return _CONFIG_PATH


def _load():
    path = _config_path()
    data = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    cfg = dict(_DEFAULTS)
    cfg.update(data)
    return cfg


def _save(cfg):
    path = _config_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_token(create=True):
    """返回当前令牌；create=True 且尚无令牌时自动生成并持久化"""
    with _LOCK:
        cfg = _load()
        if not cfg.get('token'):
            if not create:
                return None
            cfg['token'] = secrets.token_hex(16)
            _save(cfg)
        return cfg['token']


def regenerate_token():
    """重新生成令牌（旧令牌立即失效）"""
    with _LOCK:
        cfg = _load()
        cfg['token'] = secrets.token_hex(16)
        _save(cfg)
        return cfg['token']


def is_enabled():
    return bool(_load().get('enabled', True))


def set_enabled(value):
    with _LOCK:
        cfg = _load()
        cfg['enabled'] = bool(value)
        _save(cfg)
        return cfg['enabled']


def verify(presented):
    """校验请求方出示的令牌；服务停用或令牌缺失/不符均返回 False"""
    if not is_enabled():
        return False
    expected = get_token(create=False)
    if not expected or not presented:
        return False
    return secrets.compare_digest(str(expected), str(presented))
