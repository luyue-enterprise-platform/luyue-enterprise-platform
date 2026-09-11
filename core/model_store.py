# -*- coding: utf-8 -*-
"""OCR 模型仓库（v2.4.0 模型热更新基建·阶段一）

模型与代码解耦（见《OCR模型迭代机制技术方案分析.md》方案一）：
- 出厂基线模型随软件打包（rapidocr 包内 models/，冻结态位于 _internal）；
- 外置模型目录 <数据目录>/模型/ 存放热更新后的模型与 manifest.json；
- 加载顺序：外置 manifest 存在且逐文件 SHA256 校验通过 → 用外置模型；
  否则回退内置基线（引擎走 rapidocr 默认解析，不传显式路径）；
- 更新流程：下载 zip → 整包 SHA256 → 解压到 staging → 逐文件 SHA256 →
  轮换（当前版本移入 backup/，staging 提升为当前），保留上一版可回滚；
- 温更新：OCR 引擎线程本地常驻内存，替换模型后须重启平台才加载新模型
  （避免同进程内新旧模型混跑），属设计内行为。

manifest 结构（本地与远端同构，远端多出包下载字段）：
{
  "model_version": "2026.09.11-1",        # 人类可读版本
  "model_version_code": 1,                # 整数版本码，单调递增
  "min_platform_version_code": 2308,      # 要求的最低平台版本（仅远端生效）
  "models": {
    "det": {"file": "xxx.onnx", "sha256": "..."},
    "cls": {"file": "xxx.onnx", "sha256": "..."},
    "rec": {"file": "xxx.onnx", "sha256": "..."}
  },
  "pack_url": "https://.../model_pack_xxx.zip",   # 仅远端
  "pack_sha256": "...",                            # 仅远端
  "pack_size": 16000000,                           # 仅远端
  "applied_at": "2026-09-11T18:00:00",            # 仅本地（应用时间）
  "notes": "本版说明"
}
"""
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime

from core.paths import data_dir

# 外置模型目录名（与「浏览器引擎」同级的安装目录旁数据目录）
MODEL_DIR_NAME = '模型'
# 模型槽位：det 检测 / cls 方向 / rec 识别（与 rapidocr 三模型对应）
MODEL_SLOTS = ('det', 'cls', 'rec')

_LOCK = threading.RLock()


def model_root():
    """外置模型根目录：<数据目录>/模型/"""
    return os.path.join(data_dir(), MODEL_DIR_NAME)


def manifest_path():
    """当前生效的 manifest 路径"""
    return os.path.join(model_root(), 'manifest.json')


def backup_dir():
    """上一版模型备份目录（回滚用）"""
    return os.path.join(model_root(), 'backup')


def staging_root():
    """更新中转目录（下载解压、校验通过后才提升为当前）"""
    return os.path.join(model_root(), 'staging')


def read_manifest():
    """读取当前生效的 manifest；不存在/损坏返回 None"""
    with _LOCK:
        try:
            with open(manifest_path(), encoding='utf-8') as f:
                m = json.load(f)
            if not isinstance(m, dict) or not isinstance(m.get('models'), dict):
                return None
            return m
        except Exception:
            return None


def sha256_of(path, _bufsize=1024 * 1024):
    """计算文件 SHA256（流式）"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(_bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_manifest_files(manifest, base_dir):
    """校验 manifest 声明的模型文件是否齐全且 SHA256 一致

    返回 (ok: bool, issues: list[str])——ok=False 时调用方必须回退基线。
    """
    issues = []
    models = manifest.get('models') or {}
    for slot in MODEL_SLOTS:
        entry = models.get(slot)
        if not isinstance(entry, dict) or not entry.get('file') or not entry.get('sha256'):
            issues.append('%s 模型声明缺失（file/sha256）' % slot)
            continue
        # 防目录穿越：文件名须为纯文件名，不含路径分隔符
        fname = str(entry['file'])
        if os.path.basename(fname) != fname:
            issues.append('%s 文件名非法: %s' % (slot, fname))
            continue
        path = os.path.join(base_dir, fname)
        if not os.path.isfile(path):
            issues.append('%s 模型文件不存在: %s' % (slot, fname))
            continue
        actual = sha256_of(path)
        if actual.lower() != str(entry['sha256']).lower():
            issues.append('%s 模型校验不符（期望 %s… 实际 %s…）'
                          % (slot, str(entry['sha256'])[:8], actual[:8]))
    return (not issues), issues


def current_model_info():
    """当前模型状态（供 UI/状态端点）：版本 + 校验是否通过"""
    m = read_manifest()
    if not m:
        return {
            'source': '内置基线',
            'model_version': '出厂内置',
            'model_version_code': 0,
            'verified': True,
        }
    ok, issues = verify_manifest_files(m, model_root())
    return {
        'source': '外置模型',
        'model_version': m.get('model_version') or '未知',
        'model_version_code': int(m.get('model_version_code') or 0),
        'verified': ok,
        'issues': issues,
        'applied_at': m.get('applied_at'),
        'notes': m.get('notes') or '',
    }


def resolve_model_paths():
    """解析实际可用的模型路径（v2.4.0 模型热更新的引擎接入点）

    外置 manifest 存在且逐文件校验通过 → 返回 {'det':..., 'cls':..., 'rec':...}；
    否则返回 None（引擎走 rapidocr 默认解析 = 打包内置基线）。
    校验失败不抛异常：坏模型自动降级基线，平台永远可用。
    """
    m = read_manifest()
    if not m:
        return None
    ok, _issues = verify_manifest_files(m, model_root())
    if not ok:
        return None
    models = m['models']
    return {slot: os.path.join(model_root(), models[slot]['file'])
            for slot in MODEL_SLOTS}


def engine_model_kwargs():
    """生成传给 RapidOCR 的模型路径参数（无外置模型时返回空 dict）"""
    paths = resolve_model_paths()
    if not paths:
        return {}
    return {
        'det_model_path': paths['det'],
        'cls_model_path': paths['cls'],
        'rec_model_path': paths['rec'],
    }


def apply_staging(staging_dir, new_manifest):
    """把校验通过的 staging 目录提升为当前版本（原子轮换）

    步骤：清空旧 backup → 当前模型文件+manifest 移入 backup →
    staging 文件+新 manifest 移入模型根目录。全程同卷 rename，快且可恢复。
    new_manifest 会补记 applied_at 后写为当前 manifest。
    """
    with _LOCK:
        root = model_root()
        os.makedirs(root, exist_ok=True)
        old_manifest = read_manifest()

        # 1) 重建 backup：清旧 → 移入当前版本
        if os.path.isdir(backup_dir()):
            shutil.rmtree(backup_dir(), ignore_errors=True)
        os.makedirs(backup_dir(), exist_ok=True)
        if old_manifest:
            old_files = [e['file'] for e in (old_manifest.get('models') or {}).values()
                         if isinstance(e, dict) and e.get('file')]
            for fname in old_files:
                src = os.path.join(root, fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(backup_dir(), fname))
            with open(os.path.join(backup_dir(), 'manifest.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(old_manifest, f, ensure_ascii=False, indent=2)

        # 2) staging → 当前
        models = new_manifest.get('models') or {}
        for slot in MODEL_SLOTS:
            fname = models.get(slot, {}).get('file')
            if not fname:
                raise RuntimeError('新 manifest 缺少 %s 模型声明' % slot)
            src = os.path.join(staging_dir, fname)
            if not os.path.isfile(src):
                raise RuntimeError('staging 缺少 %s 模型文件: %s' % (slot, fname))
            shutil.move(src, os.path.join(root, fname))
        applied = dict(new_manifest)
        applied['applied_at'] = datetime.now().isoformat(timespec='seconds')
        with open(manifest_path(), 'w', encoding='utf-8') as f:
            json.dump(applied, f, ensure_ascii=False, indent=2)
        return applied


def rollback():
    """回滚到 backup 中的上一版模型（无可回滚版本时报错）"""
    with _LOCK:
        backup_manifest = os.path.join(backup_dir(), 'manifest.json')
        if not os.path.isfile(backup_manifest):
            raise RuntimeError('没有可回滚的历史模型版本')
        with open(backup_manifest, encoding='utf-8') as f:
            old = json.load(f)
        root = model_root()
        # 当前版本先挪进 staging（相当于反向轮换的中转）
        os.makedirs(staging_root(), exist_ok=True)
        cur = read_manifest() or {}
        for entry in (cur.get('models') or {}).values():
            fname = entry.get('file') if isinstance(entry, dict) else None
            if fname:
                src = os.path.join(root, fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(staging_root(), fname))
        if os.path.isfile(manifest_path()):
            shutil.move(manifest_path(), os.path.join(staging_root(), 'manifest.json'))
        # backup → 当前
        for entry in (old.get('models') or {}).values():
            fname = entry.get('file') if isinstance(entry, dict) else None
            if fname:
                src = os.path.join(backup_dir(), fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(root, fname))
        shutil.move(backup_manifest, manifest_path())
        return old
