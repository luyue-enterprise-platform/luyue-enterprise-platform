# -*- coding: utf-8 -*-
"""OCR 模型热更新器（v2.4.0 阶段一）

远端清单与模型包均走 COS（与应用升级同通道、同域名白名单）：
- manifest: <COS>/ocr_model/manifest.json（CacheControl 短缓存）
- 模型包:   manifest.pack_url（须与 COS 白名单前缀一致，防滥用）

流程（后台线程）：
checking（拉远端清单比对版本）→ downloading（分块下载 + 进度）→
verifying（整包 SHA256 → 解压 staging → 逐文件 SHA256）→
applying（原子轮换：当前→backup，staging→当前）→ restart_required
（温更新：须重启平台后新模型才加载，此处只提示不代重启——重启时机交给用户）

失败安全：任何一步失败 → 状态置 error + 明确原因；模型目录保持原样
（staging 残留无害，下次更新前清空），当前可用版本不受影响。
"""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime

from core import model_store

# 远端清单地址（COS，与自动升级同一分发域）
MANIFEST_URL = ('https://luyue-1466112667.cos.ap-shanghai.myqcloud.com'
                '/ocr_model/manifest.json')
# 下载白名单前缀：模型包必须落在 COS 同域（防下载地址被篡改指向任意源）
ALLOWED_PREFIX = 'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/'

# 下载分块大小与超时（模型包 ~16MB，几十秒内完成）
_CHUNK = 256 * 1024
_TIMEOUT = 60

# 状态机：idle / checking / downloading / verifying / applying /
#          restart_required / error
_state = {
    'status': 'idle',
    'percent': 0,
    'downloaded': 0,
    'total': 0,
    'remote_version': '',
    'remote_version_code': 0,
    'update_available': False,
    'last_error': '',
    'last_check_at': '',
    'finished_at': '',
}
_lock = threading.RLock()


def _set(**fields):
    with _lock:
        _state.update(fields)


def snapshot():
    """状态快照（供进度端点）"""
    with _lock:
        return dict(_state)


def _fetch_json(url, timeout=_TIMEOUT):
    """拉取远端 manifest（小文件，直接整体读）"""
    req = urllib.request.Request(url, headers={'User-Agent': 'LuyueApp/2.4.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _download(url, dest_path):
    """分块下载到 dest_path，实时回报进度；返回 Content-Length"""
    req = urllib.request.Request(url, headers={'User-Agent': 'LuyueApp/2.4.0'})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        total = int(resp.headers.get('Content-Length') or 0)
        downloaded = 0
        with open(dest_path, 'wb') as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                _set(downloaded=downloaded, total=total,
                     percent=min(99, int(downloaded * 100 / total)) if total else 0)
        return total


def check_remote(platform_version_code):
    """检查远端是否有可用模型更新（同步调用，返回检查结果 dict）

    - 拉远端 manifest；网络失败 → up_to_date=False + error（不改变本地任何状态）
    - 远端 model_version_code <= 本地 → 无更新
    - 远端 min_platform_version_code > 平台版本 → 提示先升级软件
    - pack_url 不在白名单 → 拒绝（安全校验）
    """
    _set(status='checking')
    try:
        remote = _fetch_json(MANIFEST_URL)
    except Exception as e:
        _set(status='idle', last_error='清单拉取失败: %s' % e)
        return {'ok': False, 'error': '无法获取模型更新清单（网络异常）: %s' % e}
    finally:
        pass
    _set(last_check_at=datetime.now().isoformat(timespec='seconds'))

    local = model_store.read_manifest() or {}
    local_code = int(local.get('model_version_code') or 0)
    remote_code = int(remote.get('model_version_code') or 0)
    min_platform = int(remote.get('min_platform_version_code') or 0)
    pack_url = str(remote.get('pack_url') or '')

    result = {
        'ok': True,
        'remote_version': remote.get('model_version') or '未知',
        'remote_version_code': remote_code,
        'local_version': (local.get('model_version') if local else '出厂内置'),
        'local_version_code': local_code,
        'notes': remote.get('notes') or '',
    }
    if remote_code <= local_code:
        result['update_available'] = False
        _set(status='idle', update_available=False, remote_version='',
             remote_version_code=0)
        return result
    if min_platform > int(platform_version_code or 0):
        result['update_available'] = False
        result['error'] = ('新模型要求平台版本 ≥ v%s（当前 v%s），请先升级软件'
                           % (min_platform, platform_version_code))
        _set(status='idle')
        return result
    if not pack_url.startswith(ALLOWED_PREFIX) or not pack_url.lower().endswith('.zip'):
        result['update_available'] = False
        result['error'] = '模型包下载地址不合法（不在白名单内），已拒绝'
        _set(status='idle')
        return result
    result['update_available'] = True
    result['pack_url'] = pack_url
    _set(status='idle', update_available=True,
         remote_version=result['remote_version'],
         remote_version_code=remote_code)
    return result


def start_update(platform_version_code):
    """启动后台模型更新（重复启动会被拒绝）；返回 (ok, error)"""
    with _lock:
        if _state['status'] in ('downloading', 'verifying', 'applying', 'checking'):
            return False, '已有模型更新任务进行中（%s）' % _state['status']
        _state.update({'status': 'checking', 'percent': 0, 'downloaded': 0,
                       'total': 0, 'last_error': ''})
    t = threading.Thread(target=_run, args=(platform_version_code,),
                         daemon=True)
    t.start()
    return True, ''


def _run(platform_version_code):
    """后台主流程：检查 → 下载 → 校验 → 应用 → 提示重启"""
    zip_path = None
    try:
        chk = check_remote(platform_version_code)
        if not chk.get('ok') or not chk.get('update_available'):
            raise RuntimeError(chk.get('error') or '没有可用的模型更新')

        remote = _fetch_json(MANIFEST_URL)
        pack_url = remote['pack_url']
        pack_sha256 = str(remote.get('pack_sha256') or '').lower()
        pack_size = int(remote.get('pack_size') or 0)

        # 下载（临时文件 → 模型目录 download.zip）
        _set(status='downloading')
        os.makedirs(model_store.model_root(), exist_ok=True)
        fd, zip_path = tempfile.mkstemp(prefix='ly_model_', suffix='.zip',
                                        dir=model_store.model_root())
        os.close(fd)
        total = _download(pack_url, zip_path)

        # 整包校验
        _set(status='verifying', percent=100)
        fsize = os.path.getsize(zip_path)
        if pack_size and fsize != pack_size:
            raise RuntimeError('模型包大小不符（期望 %d，实际 %d），可能下载不完整'
                               % (pack_size, fsize))
        if pack_sha256:
            h = hashlib.sha256()
            with open(zip_path, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            if h.hexdigest().lower() != pack_sha256:
                raise RuntimeError('模型包 SHA256 校验失败，文件可能被篡改或损坏')

        # 解压到 staging（先清空旧残留）
        staging = model_store.staging_root()
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                # 防目录穿越：只接受纯文件名
                if os.path.basename(name) != name or name.startswith('.'):
                    raise RuntimeError('模型包含法条目: %s' % name)
            zf.extractall(staging)

        # 逐文件校验（manifest 声明 vs staging 实际）
        ok, issues = model_store.verify_manifest_files(remote, staging)
        if not ok:
            raise RuntimeError('模型文件校验失败: %s' % '；'.join(issues))

        # 原子轮换：当前 → backup，staging → 当前
        _set(status='applying')
        model_store.apply_staging(staging, remote)

        # 温更新：不代重启，提示用户重启后生效
        _set(status='restart_required', finished_at=datetime.now().isoformat(
            timespec='seconds'))
    except Exception as e:
        _set(status='error', last_error=str(e),
             finished_at=datetime.now().isoformat(timespec='seconds'))
    finally:
        if zip_path and os.path.isfile(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass


def do_rollback():
    """回滚到上一版模型（须重启后生效）"""
    with _lock:
        if _state['status'] in ('downloading', 'verifying', 'applying'):
            return False, '模型更新进行中，暂不能回滚'
    try:
        old = model_store.rollback()
    except Exception as e:
        _set(status='error', last_error='回滚失败: %s' % e)
        return False, str(e)
    _set(status='restart_required',
         last_error='',
         finished_at=datetime.now().isoformat(timespec='seconds'))
    return True, old.get('model_version') or '上一版'
