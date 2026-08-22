# -*- coding: utf-8 -*-
"""
医保参保证明批量下载模块 — Flask Blueprint
（移植自独立工具「医保证明下载工具」的 gui.py）

工作流: 上传 Excel 花名册 → 启动 Chromium（有头）→ 用户手动登录陕西医保网厅
        → 程序自动导航到职工参保证明查询打印页 → 逐人查询并下载 PDF
        → 文件保存到 数据目录/outputs/medical/日期/ 下以姓名命名

关键技术约束:
- Playwright 的 browser/page/context 对象绑定到创建它的事件循环，
  登录与下载必须在同一线程、同一事件循环中执行（_main_worker 两阶段设计）
- SSE: Response 必须传入生成器对象（event_stream()），不能传函数
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty

from flask import (
    Blueprint, render_template, request, jsonify,
    Response, send_file
)

from core.auth import login_required

from .core import config
from .core.excel_reader import read_excel, Employee
from .core.downloader import (
    MedicalInsuranceDownloader,
    STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED,
)

# ===== 路径设置 =====
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'medical')
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== Blueprint 创建 =====
medical_bp = Blueprint(
    'medical',
    __name__,
    url_prefix='/medical',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static')
)

UPLOAD_DIR = Path(config.UPLOAD_DIR)
LOG_DIR = Path(config.LOG_DIR)

# ===== 文件日志（落盘，便于排查问题） =====

_log_file = LOG_DIR / "medical.log"
_log_handlers = []
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(str(_log_file), encoding="utf-8"))
except Exception:
    # 日志文件不可写（被其它进程占用/权限不足）时降级：
    # 不阻塞主流程，仅保留内存日志（程序照常运行，排查问题仍可看界面日志）
    pass
if not _log_handlers:
    _log_handlers.append(logging.NullHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt=config.LOG_DATE_FORMAT,
    handlers=_log_handlers,
)
_file_logger = logging.getLogger("medical")

_LEVEL_MAP = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "success": logging.INFO,
}


# ===== 全局状态 =====

class AppState:
    def __init__(self):
        self.employees: list[Employee] = []
        self.downloader: MedicalInsuranceDownloader | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.worker_thread: threading.Thread | None = None
        self.is_running = False
        self.should_stop = False
        self.logged_in = False

        # 事件队列（SSE 推送用）
        self.event_queue: Queue = Queue()

        # 用户手动确认标志
        self._login_confirmed = False      # 手动确认已登录
        self._manual_print_confirmed = False  # 手动确认已另存为PDF
        self._download_requested = False   # 下载请求信号（通知主工作线程开始下载）

        # 统计
        self.stats = {"success": 0, "skipped": 0, "failed": 0, "total": 0, "current": 0}

        # 当前状态
        self.current_status = "就绪"

    def push_event(self, event_type: str, data: dict):
        """推送事件到 SSE 队列"""
        self.event_queue.put({"type": event_type, "data": data, "ts": time.time()})

    def reset_stats(self):
        self.stats = {"success": 0, "skipped": 0, "failed": 0, "total": 0, "current": 0}


state = AppState()


# ===== 事件回调 =====

def on_log_callback(level: str, msg: str):
    """下载器日志回调"""
    # 写入文件日志（便于排查问题）
    try:
        _file_logger.log(_LEVEL_MAP.get(level, logging.INFO), msg)
    except Exception:
        pass

    state.push_event("log", {
        "level": level,
        "message": msg,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })


def on_progress_callback(current, total, emp, status, message):
    """下载器进度回调"""
    state.stats["current"] = current
    state.stats["total"] = total

    if status == STATUS_SUCCESS:
        state.stats["success"] += 1
    elif status == STATUS_SKIPPED:
        state.stats["skipped"] += 1
    elif status == STATUS_FAILED:
        state.stats["failed"] += 1

    state.current_status = f"[{current}/{total}] {emp.display} - {message}"

    state.push_event("progress", {
        "current": current,
        "total": total,
        "name": emp.name,
        "id_card": emp.id_card,
        "status": status,
        "message": message,
        "stats": state.stats.copy(),
    })


# ===== Flask 路由 =====

@medical_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('medical_index.html')


@medical_bp.route('/api/load-excel', methods=['POST'])
def load_excel():
    """上传并解析 Excel 员工名单"""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "未提供文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"ok": False, "error": "未选择文件"}), 400

    # 保存上传的文件到上传目录
    upload_dir = UPLOAD_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 防路径穿越：仅取文件名部分
    safe_name = Path(file.filename).name
    file_path = str(upload_dir / safe_name)
    file.save(file_path)

    try:
        employees, errors = read_excel(file_path)
        state.employees = employees

        return jsonify({
            "ok": True,
            "count": len(employees),
            "errors": errors[:20],
            "employees": [{"name": e.name, "id_card": e.id_card[-4:], "phone": e.phone, "remark": e.remark} for e in employees[:50]],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@medical_bp.route('/api/login', methods=['POST'])
def start_login():
    """启动浏览器并等待登录"""
    if state.is_running:
        return jsonify({"ok": False, "error": "程序正在运行中"}), 400

    # 若上一轮主工作线程仍在存活（Phase 2 等待中），先停止它
    if state.worker_thread and state.worker_thread.is_alive():
        state.should_stop = True
        state.worker_thread.join(timeout=5)
        state.should_stop = False

    state.is_running = True
    state.should_stop = False
    state.logged_in = False
    state._login_confirmed = False
    state._download_requested = False

    if not config.BROWSER_ENGINE_DIR:
        on_log_callback("warn", "未找到「浏览器引擎」目录，将尝试使用 Playwright 默认浏览器路径")

    state.worker_thread = threading.Thread(target=_main_worker, daemon=True)
    state.worker_thread.start()

    return jsonify({"ok": True, "message": "浏览器启动中..."})


@medical_bp.route('/api/download', methods=['POST'])
def start_download():
    """开始批量下载"""
    if not state.logged_in or not state.downloader:
        return jsonify({"ok": False, "error": "请先登录网厅"}), 400

    if not state.employees:
        return jsonify({"ok": False, "error": "请先加载员工名单"}), 400

    if state.is_running:
        return jsonify({"ok": False, "error": "程序正在运行中"}), 400

    # 不启动新线程——Playwright 对象绑定到主工作线程的事件循环，
    # 跨线程/跨循环使用会报错。改为通知主工作线程在原循环中执行下载。
    state.is_running = True
    state.should_stop = False
    state.reset_stats()
    state.stats["total"] = len(state.employees)
    state._download_requested = True

    return jsonify({"ok": True, "message": "开始下载...", "total": len(state.employees)})


@medical_bp.route('/api/stop', methods=['POST'])
def stop():
    """停止运行"""
    state.should_stop = True
    on_log_callback("warn", "用户请求停止...")
    return jsonify({"ok": True})


@medical_bp.route('/api/status')
def status():
    """获取当前状态"""
    return jsonify({
        "is_running": state.is_running,
        "logged_in": state.logged_in,
        "employee_count": len(state.employees),
        "stats": state.stats,
        "current_status": state.current_status,
    })


@medical_bp.route('/api/events')
def events():
    """SSE 事件流"""
    def event_stream():
        while True:
            try:
                event = state.event_queue.get(timeout=1)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Empty:
                yield f"data: {json.dumps({'type': 'ping', 'ts': time.time()})}\n\n"

    # 注意：必须调用 event_stream() 传入生成器，传函数对象会导致
    # TypeError: 'function' object is not iterable（SSE 断连，前端无响应）
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@medical_bp.route('/api/open-template')
def download_template():
    """下载 Excel 模板"""
    template_path = config.TEMPLATE_DIR / "员工名单模板.xlsx"
    if template_path.exists():
        return send_file(str(template_path), as_attachment=True, download_name="员工名单模板.xlsx")
    return jsonify({"ok": False, "error": "模板文件不存在"}), 404


@medical_bp.route('/api/confirm-login', methods=['POST'])
def confirm_login():
    """用户手动确认已在浏览器中完成登录"""
    state._login_confirmed = True
    on_log_callback("info", "已收到手动确认：登录完成")
    return jsonify({"ok": True})


@medical_bp.route('/api/confirm-manual-print', methods=['POST'])
def confirm_manual_print():
    """用户确认已手动下载/另存为PDF（自动下载失败时的兜底）"""
    state._manual_print_confirmed = True
    on_log_callback("info", "已收到手动确认：PDF 已保存，继续下一位")
    return jsonify({"ok": True})


# ===== 工作线程 =====

def _main_worker():
    """
    主工作线程 — 登录与下载共用同一个事件循环。

    关键约束: Playwright 的 browser/page/context 对象绑定到创建它的事件循环，
    不能跨事件循环使用。因此登录和下载必须在同一线程、同一循环中执行。

    Phase 1: 启动浏览器 → 等待登录
    Phase 2: 登录成功后保持循环存活，轮询下载信号 → 收到信号后执行批量下载
    """
    async def _run():
        # ---- Phase 1: 启动浏览器并等待登录 ----
        state.downloader = MedicalInsuranceDownloader(
            on_log=on_log_callback,
            on_progress=on_progress_callback,
            headless=False,
            on_stop_check=lambda: state.should_stop,
        )

        try:
            await state.downloader.start()
            on_log_callback("info", "浏览器已打开，请在浏览器中完成登录")
            on_log_callback("info", "登录完成后，点击下方「✅ 我已登录，开始下载」按钮")

            logged_in = await state.downloader.wait_for_login(
                on_manual_confirm=lambda: state._login_confirmed
            )

            if logged_in:
                state.logged_in = True
                on_log_callback("info", "✅ 登录成功！可以开始批量下载了")
                state.push_event("login_done", {"success": True})
            elif state.should_stop:
                on_log_callback("info", "已停止（未完成登录）")
                state.push_event("login_done", {"success": False})
                return
            else:
                on_log_callback("error", "登录超时或失败")
                state.push_event("login_done", {"success": False})
                return

        except Exception as e:
            on_log_callback("error", f"启动失败: {e}")
            state.push_event("login_done", {"success": False})
            state.is_running = False
            return

        # 登录阶段结束，标记空闲（等待下载请求）
        state.is_running = False

        # ---- Phase 2: 等待下载请求（保持事件循环存活，Playwright 对象可用） ----
        while not state.should_stop:
            if state._download_requested:
                state._download_requested = False
                state.is_running = True

                try:
                    # 自动导航到目标页面
                    on_log_callback("info", "正在导航到查询打印页面...")
                    navigated = await state.downloader.navigate_to_target()

                    if not navigated:
                        on_log_callback("error", "导航到目标页面失败")
                        state.push_event("download_done", state.stats.copy())
                        continue

                    if state.should_stop:
                        on_log_callback("info", "已停止")
                        state.push_event("download_done", state.stats.copy())
                        continue

                    # 批量下载
                    total = len(state.employees)
                    on_log_callback("info", f"开始批量下载，共 {total} 人")

                    stopped = False
                    for i, emp in enumerate(state.employees, start=1):
                        if state.should_stop:
                            stopped = True
                            break

                        state._manual_print_confirmed = False
                        await state.downloader.download_one(
                            emp, i, total,
                            on_manual_print_confirm=lambda: state._manual_print_confirmed,
                            on_manual_print_request=lambda m: state.push_event("print_wait", {"message": m}),
                        )

                        if i < total and not state.should_stop:
                            await asyncio.sleep(1.5)

                    if stopped:
                        on_log_callback("info", "用户已停止下载")
                        state.push_event("stopped", state.stats.copy())
                    else:
                        on_log_callback("info", f"下载完成！成功 {state.stats['success']}，跳过 {state.stats['skipped']}，失败 {state.stats['failed']}")
                    state.push_event("download_done", state.stats.copy())

                except Exception as e:
                    on_log_callback("error", f"下载错误: {e}")
                    state.push_event("download_done", state.stats.copy())
                finally:
                    state.is_running = False

            await asyncio.sleep(0.5)

        # 工作线程退出（收到停止信号），关闭浏览器释放资源
        if state.downloader:
            try:
                await state.downloader.close()
            except Exception:
                pass

    try:
        state.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(state.loop)
        state.loop.run_until_complete(_run())
    except Exception as e:
        on_log_callback("error", f"运行错误: {e}")
        state.is_running = False
        state.push_event("login_done", {"success": False})
