# -*- coding: utf-8 -*-
"""MCP 任务登记表

业务处理耗时较长（OCR/转换/批量归档），MCP 工具统一走三步式：
    提交（*_run 返回 task_id）→ get_task_status 轮询 → get_task_result 取结果

社保能力直接复用宿模块原生任务表（modules.insurance.blueprint.tasks），
本表通过 native_task_id 记录映射，查询时由原生解析器实时回读进度。

v2.4.1 防重复提交：执行指纹登记表——同一批参数的任务正在执行时，
后续同参数确认被拦截并指向既有任务，避免 AI 超时误重试导致
重复任务并发抢占 CPU（onnxruntime 单会话已吃满核，多开净负优化）。
"""
import threading
import uuid
from datetime import datetime

_LOCK = threading.Lock()
_TASKS = {}

# 执行指纹 -> task_id（仅登记"已开始执行"的任务；任务到达终态自动清除）
_FINGERPRINTS = {}

# 状态：pending / processing / success / error / cancelled
_TERMINAL = ('success', 'error', 'cancelled')


def register_fingerprint(fingerprint, task_id):
    """登记执行指纹（任务即将开始执行时调用）"""
    with _LOCK:
        _FINGERPRINTS[fingerprint] = task_id


def find_active_by_fingerprint(fingerprint):
    """查找同指纹且仍在执行（pending/processing）的任务；无则返回 None。

    指向终态/waiting_confirm 任务的指纹属残留，顺手清理（自愈）。
    """
    with _LOCK:
        tid = _FINGERPRINTS.get(fingerprint)
        if not tid:
            return None
        task = _TASKS.get(tid)
        if task and task.get('status') in ('pending', 'processing'):
            return tid
        _FINGERPRINTS.pop(fingerprint, None)
        return None


def _clear_fingerprints_of(task_id):
    """清除指向该任务的全部指纹（终态时调用）"""
    with _LOCK:
        for fp, tid in list(_FINGERPRINTS.items()):
            if tid == task_id:
                _FINGERPRINTS.pop(fp, None)


def create(capability, params=None):
    """登记一个新任务，返回 task_id"""
    task_id = uuid.uuid4().hex[:8]
    with _LOCK:
        _TASKS[task_id] = {
            'task_id': task_id,
            'capability': capability,
            'status': 'pending',
            'current': 0,
            'total': 0,
            'progress': 0,
            'message': '等待处理...',
            'result': None,
            'error': None,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'native_task_id': None,
            'params': params or {},
        }
    return task_id


def update(task_id, **fields):
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return None
        task.update(fields)
        # 自动维护进度百分比
        total = task.get('total') or 0
        if total > 0:
            task['progress'] = int(min(100, round(
                (task.get('current') or 0) * 100.0 / total)))
        return dict(task)


def get(task_id):
    with _LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def exists(task_id):
    with _LOCK:
        return task_id in _TASKS


def set_native(task_id, native_id):
    return update(task_id, native_task_id=native_id)


def finish(task_id, result=None, message='处理完成'):
    _clear_fingerprints_of(task_id)
    return update(task_id, status='success', result=result, message=message,
                  current=(get(task_id) or {}).get('total') or 0, progress=100)


def fail(task_id, error, message='处理失败'):
    _clear_fingerprints_of(task_id)
    return update(task_id, status='error', error=str(error), message=message)


def public_view(task):
    """对外暴露的任务视图（去掉内部参数，保留 AI 需要的字段）"""
    if not task:
        return None
    keys = ('task_id', 'capability', 'status', 'current', 'total', 'progress',
            'message', 'error', 'created_at', 'result')
    return {k: task.get(k) for k in keys if task.get(k) is not None or k in
            ('task_id', 'capability', 'status', 'message')}
