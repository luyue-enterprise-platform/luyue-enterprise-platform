# -*- coding: utf-8 -*-
"""
陕西省医保网厅 - 参保证明自动下载核心模块
基于 Playwright 实现浏览器自动化

工作流程:
1. 启动浏览器（有头模式，用户可见）
2. 用户手动登录网厅（处理验证码、选择单位）
3. 脚本接管，批量下载参保证明
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

from . import config
from .excel_reader import Employee


# ==================== 回调类型 ====================

# 进度回调: (current, total, employee, status, message)
ProgressCallback = Callable[[int, int, Employee, str, str], None]
# 日志回调: (level, message)
LogCallback = Callable[[str, str], None]

# 状态常量
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"




class MedicalInsuranceDownloader:
    """医保参保证明批量下载器"""

    def __init__(
        self,
        on_progress: Optional[ProgressCallback] = None,
        on_log: Optional[LogCallback] = None,
        headless: bool = False,
        on_stop_check: Optional[Callable[[], bool]] = None,
    ):
        """
        Args:
            on_progress: 进度回调
            on_log: 日志回调
            headless: 是否无头模式（建议 False，用户需要手动登录）
            on_stop_check: 停止检查回调，返回 True 表示用户请求停止
        """
        self.on_progress = on_progress or (lambda *a: None)
        self.on_log = on_log or (lambda lvl, msg: None)
        self.headless = headless
        self._stop_check = on_stop_check or (lambda: False)

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        # 下载目录（固定为 exe 旁的 downloads/日期/，无需用户选择）
        self._download_dir = config.DOWNLOAD_DIR / get_download_subdir()
        self._download_dir.mkdir(parents=True, exist_ok=True)

        self._log("info", f"文件保存目录: {self._download_dir}")

        # 手动保存PDF兜底（_need_manual_print 事件已通过回调上报，等待用户确认）
        self._manual_print_waiting = False

    def _stopped(self) -> bool:
        """是否收到停止请求"""
        try:
            return bool(self._stop_check())
        except Exception:
            return False

    def _log(self, level: str, msg: str):
        """记录日志"""
        timestamp = datetime.now().strftime(config.LOG_DATE_FORMAT)
        self.on_log(level, f"{msg}")

    async def start(self):
        """启动浏览器，打开网厅"""
        self._log("info", "正在启动浏览器...")

        self.playwright = await async_playwright().start()

        # 下载路径配置
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",  # 隐藏自动化特征
                "--no-sandbox",
                "--start-maximized",
            ],
        )

        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        self.context.set_default_timeout(config.ELEMENT_TIMEOUT)
        self.context.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT)

        self.page = await self.context.new_page()

        # 导航到网厅首页
        # 注意：网厅首页有常驻监控/统计请求，networkidle 几乎不可能在 30s 内达成，
        # 因此使用 domcontentloaded + 固定等待，避免启动即超时。
        self._log("info", f"正在打开网厅: {config.PORTAL_URL}")
        try:
            await self.page.goto(config.PORTAL_URL, wait_until="domcontentloaded")
        except Exception as e:
            self._log("warn", f"导航到网厅首页异常: {str(e)[:100]}，继续等待页面稳定...")
        await asyncio.sleep(2)
        self._log("info", "网厅页面已打开")

        return self.page

    async def wait_for_login(self, check_interval: float = 2.0, on_manual_confirm=None) -> bool:
        """
        等待用户手动登录

        检测登录成功的标志（任一命中即认为登录成功）：
        1. 页面出现"报表打印/网上经办/更多服务/职工参保证明"等网厅菜单（≥2个）
        2. 页面出现"退出登录/退出/注销"等登录态标志
        3. URL 变化为网厅内部路径（非首页）
        4. 用户在前端点击「我已登录」手动确认（自动检测失灵的兜底）

        Args:
            check_interval: 轮询间隔（秒）
            on_manual_confirm: 手动确认回调，返回 True 表示用户已确认登录完成

        Returns:
            True 如果检测到登录成功
        """
        self._log("info", "=" * 50)
        self._log("info", "请在浏览器中手动完成以下操作：")
        self._log("info", "1. 点击「单位登录」")
        self._log("info", "2. 输入经办人账号（手机号）和密码")
        self._log("info", "3. 输入验证码并登录")
        self._log("info", "4. 选择要办理业务的单位")
        self._log("info", "5. 点击「进入单位网厅」")
        self._log("info", "=" * 50)
        self._log("info", "等待登录完成...")
        self._log("info", "提示：若自动检测不到，可点击页面上的「✅ 我已登录，开始下载」按钮")

        login_start = time.time()
        max_wait = 600  # 最多等待 10 分钟

        while time.time() - login_start < max_wait:
            # 用户请求停止
            if self._stopped():
                self._log("info", "已收到停止请求，中止登录等待")
                return False

            # 手动确认优先（自动检测失灵的兜底）
            if on_manual_confirm and on_manual_confirm():
                self._log("info", "用户手动确认已登录，继续执行")
                return True

            try:
                # 方法1：检查页面文本（网厅功能菜单）
                page_text = await self.page.inner_text("body", timeout=3000)

                menu_markers = [
                    "报表打印",
                    "网上经办",
                    "更多服务",
                    "职工参保证明",
                ]
                found_menus = [m for m in menu_markers if m in page_text]
                if len(found_menus) >= 2:
                    self._log("info", f"检测到已进入单位网厅（匹配到: {', '.join(found_menus)}）")
                    return True

                # 方法2：检查登录态标志（退出/注销等，说明已登录）
                login_markers = ["退出登录", "退出", "注销", "安全退出", "经办人"]
                found_login = [m for m in login_markers if m in page_text]
                if found_login:
                    self._log("info", f"检测到登录态标志: {', '.join(found_login)}")
                    return True

                # 方法3：检查 URL（hash 路由离开首页即认为进入网厅内部）
                current_url = self.page.url
                if "hallEnter" in current_url:
                    # 提取 # 后面的路由
                    hash_part = current_url.split("#", 1)[-1] if "#" in current_url else ""
                    if hash_part and hash_part not in ("", "/", "/Index", "/index"):
                        self._log("info", f"URL 变化检测到登录成功: {current_url}")
                        return True

            except Exception:
                pass

            await asyncio.sleep(check_interval)

        self._log("error", "等待登录超时（10分钟），请重新尝试")
        return False

    async def navigate_to_target(self) -> bool:
        """
        导航到参保证明查询打印页面。

        按用户要求，登录确认后程序自动跳转到目标 URL：
        https://zwfw.shaanxi.gov.cn/ggfw/hallUnit/#/staff-insu-print

        Returns:
            True 如果成功导航到目标页面
        """
        if self._stopped():
            self._log("info", "已收到停止请求，中止导航")
            return False

        self._log("info", f"正在导航到查询打印页面: {config.TARGET_URL}")
        try:
            # 目标页面同样存在后台常驻请求，使用 domcontentloaded 避免超时
            await self.page.goto(config.TARGET_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)  # 等待页面渲染完成
        except Exception as e:
            self._log("warn", f"导航到目标页面异常（可能仍在加载）: {str(e)[:100]}")

        # 验证是否到达目标页面
        if await self._is_cert_page():
            self._log("info", "✅ 已到达「职工参保证明查询打印」页面，开始批量下载")
            return True

        # 即使验证未通过也继续（页面结构可能不同，但不影响后续操作）
        self._log("info", "页面已加载，开始批量下载")
        return True

    async def _is_cert_page(self) -> bool:
        """检测当前页面（含所有 iframe）是否为目标页面"""
        markers = [
            "职工参保证明查询打印",
            "单位参保人员信息查询",
            "单位参保证明打印",
        ]
        for frame in self.page.frames:
            try:
                text = await frame.content()
                if not text:
                    continue
                # 限制长度避免异常页面拖慢检测
                text = text[:50000]
                if any(m in text for m in markers):
                    return True
            except Exception:
                continue
        return False

    async def _save_diag(self, tag: str, full_page: bool = False):
        """保存当前页面截图与各 frame 信息，用于诊断导航/操作失败"""
        try:
            diag_dir = config.LOG_DIR / "diag"
            diag_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            shot = diag_dir / f"nav_{tag}_{ts}.png"
            try:
                await self.page.screenshot(path=str(shot), full_page=full_page)
                self._log("info", f"已保存诊断截图: {shot}")
            except Exception as e:
                self._log("warn", f"截图失败: {e}")
            # 打印所有 frame 的 URL，帮助定位目标菜单在哪个 iframe
            for i, fr in enumerate(self.page.frames):
                try:
                    title = ""
                    try:
                        title = await fr.title()
                    except Exception:
                        pass
                    self._log("info", f"  frame[{i}] title={title[:40]!r} url={fr.url[:140]}")
                except Exception:
                    self._log("info", f"  frame[{i}] <无法读取>")
        except Exception as e:
            self._log("warn", f"保存诊断信息失败: {e}")

    async def download_one(
        self,
        employee: Employee,
        index: int,
        total: int,
        on_manual_print_confirm=None,
        on_manual_print_request=None,
    ) -> tuple[bool, str]:
        """
        下载单个员工的参保证明

        流程: 输入身份证号 → 查询 → 点击「下载」按钮（浏览器直接下载，自动保存到输出目录）

        Args:
            employee: 员工信息
            index: 当前序号（从1开始）
            total: 总人数
            on_manual_print_confirm: 手动保存PDF确认回调（返回 True 表示用户已保存）
            on_manual_print_request: 请求手动操作回调（用于前端弹出确认条）

        Returns:
            (是否成功, 消息)
        """
        self.on_progress(index, total, employee, STATUS_RUNNING, "开始查询...")

        try:
            # Step 1: 找到证件号码输入框并输入
            self._log("info", f"[{index}/{total}] 查询: {employee.display}")

            input_filled = await self._fill_id_input(employee.id_card)
            if not input_filled:
                msg = "未找到证件号码输入框"
                self.on_progress(index, total, employee, STATUS_FAILED, msg)
                return False, msg

            # Step 2: 点击查询按钮
            await asyncio.sleep(0.5)
            query_clicked = await self._click_query_button()
            if not query_clicked:
                msg = "未找到查询按钮"
                self.on_progress(index, total, employee, STATUS_FAILED, msg)
                return False, msg

            # Step 3: 等待查询结果
            await asyncio.sleep(2)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=config.NAVIGATION_TIMEOUT)
            except Exception:
                pass

            # Step 4: 检查是否查到人员（检测失败不再中断，继续尝试下载，
            #         否则"查询误判"会跳过所有下载环节——很多网厅页面结构
            #         无法可靠检测，直接下载最稳妥，下载结果由文件落地确认）
            found, detail = await self._check_person_found(employee)
            if not found:
                self._log("warn", f"查询检测未确认到该人员（{detail}），仍继续尝试下载")
                self.on_progress(index, total, employee, STATUS_RUNNING, "检测存疑，继续尝试下载...")
            else:
                self._log("info", f"查询检测: {detail}")

            # Step 5: 获取证明PDF（点「下载」按钮 → 浏览器直接下载 → 自动保存）
            self.on_progress(index, total, employee, STATUS_RUNNING, "正在下载PDF...")

            success, msg = await self._obtain_pdf(
                employee,
                on_manual_print_confirm=on_manual_print_confirm,
                on_manual_print_request=on_manual_print_request,
            )
            if not success:
                self.on_progress(index, total, employee, STATUS_FAILED, msg)
                return False, msg

            self.on_progress(index, total, employee, STATUS_SUCCESS, "下载成功")
            return True, msg

        except PWTimeout as e:
            msg = f"操作超时: {str(e)[:100]}"
            self.on_progress(index, total, employee, STATUS_FAILED, msg)
            return False, msg
        except Exception as e:
            msg = f"未知错误: {str(e)[:200]}"
            self.on_progress(index, total, employee, STATUS_FAILED, msg)
            return False, msg

    def _pick_save_path(self, name: str) -> Path:
        """
        生成下载文件路径：以花名册姓名命名（如 张三.pdf）；
        若同名文件已存在（同名员工/重试/历史文件），自动加序号避免覆盖：
        张三.pdf → 张三1.pdf → 张三2.pdf ...（以此类推，取第一个可用路径）
        """
        def _usable(p: Path) -> bool:
            """路径可用：不存在，或存在但为 0 字节（空文件，可复用）"""
            try:
                return not (p.exists() and p.stat().st_size > 0)
            except Exception:
                return True

        base = self._download_dir / f"{name}.pdf"
        if _usable(base):
            return base

        n = 1
        while True:
            candidate = self._download_dir / f"{name}{n}.pdf"
            if _usable(candidate):
                return candidate
            n += 1

    async def _obtain_pdf(
        self,
        employee: Employee,
        on_manual_print_confirm=None,
        on_manual_print_request=None,
    ) -> tuple[bool, str]:
        """
        获取证明PDF（真实业务约束：逐人查询后点击「下载」按钮，浏览器直接下载文件）

        网厅的「下载」按钮触发的是浏览器原生文件下载（download 事件），
        程序自动捕获该事件并把文件保存到程序固定的输出目录
        （downloads/日期/），随后自动处理下一位，全程无需人工操作。

        流程:
        1. 点击「下载」按钮（若页面无「下载」按钮，尝试点「打印」按钮兜底）；
           若弹出页面内预览弹窗（el-dialog 等），再点弹窗内的「下载」
        2. 等待浏览器下载（最长 60 秒，自动保存）：
           - download 事件：文件已开始下载 → 保存为规范文件名，自动下一位
           - popup 新窗口：预览以新窗口打开 → 尝试在新窗口点「下载」
           - 用户手动确认「已保存PDF」→ 校验文件后继续
        3. 全部失败 → 手动兜底（引导用户手动点「下载」+ 确认按钮）
        """
        # 文件名以花名册中的姓名命名（如 张三.pdf）；
        # 若同名文件已存在（如花名册中有同名员工），自动加序号: 张三1.pdf、张三2.pdf...
        save_path = self._pick_save_path(employee.name)
        self._log("info", f"本次保存文件名: {save_path.name}")

        loop = asyncio.get_running_loop()
        dl_future = loop.create_future()
        popup_future = loop.create_future()

        async def _on_download(d):
            if not dl_future.done():
                dl_future.set_result(d)

        async def _on_popup(pg):
            if not popup_future.done():
                popup_future.set_result(pg)

        # 用 once 注册（一次性），避免监听器累积导致行为混乱
        self.page.once("download", _on_download)
        self.page.once("popup", _on_popup)

        # ---------- Step 1: 只点击「单位参保人员信息列表」模块内的「下载」/「打印」按钮 ----------
        # 用户明确：下载的是"单位参保人员信息列表"模块中查询结果行内的按钮，
        # 绝不是页面其它模块（如"单位参保证明打印"）的下载按钮。
        # 通过模块卡片几何定位 + 祖先容器文本双重校验，杜绝误点其他模块。
        row_btn_clicked = await self._click_download_in_list_module(employee)
        if row_btn_clicked:
            self._log("info", "已点击查询结果行内的下载/打印按钮，等待浏览器下载...")

            if await self._click_download_in_dialog():
                self._log("info", "已在预览弹窗内点击「下载」，等待浏览器下载...")
                await asyncio.sleep(1.0)

            # Step 2: 等待下载
            wait_result = await self._wait_download_result(
                dl_future, popup_future,
                on_manual_print_confirm=on_manual_print_confirm,
                timeout=60,
            )

            if self._stopped():
                return False, "已停止"

            if wait_result == "download" and dl_future.done():
                download = dl_future.result()
                try:
                    await download.save_as(str(save_path))
                    size = save_path.stat().st_size
                    if size > 0:
                        self._log("info", f"✓ 已保存PDF: {save_path.name} ({size / 1024:.1f} KB)")
                        return True, f"已保存: {save_path.name}"
                except Exception as e:
                    self._log("warn", f"保存下载文件失败: {e}")

            if wait_result == "popup" and popup_future.done():
                popup = popup_future.result()
                self._log("info", f"检测到预览窗口: {popup.url}")
                try:
                    await popup.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                try:
                    popup_clicked = await self._click_download_button_on(popup)
                    if popup_clicked:
                        self._log("info", "预览窗口中点击了下载按钮，等待下载...")
                        wr = await self._wait_download_result(
                            dl_future, popup_future,
                            on_manual_print_confirm=on_manual_print_confirm,
                            timeout=60,
                        )
                        if wr == "download" and dl_future.done():
                            download = dl_future.result()
                            await download.save_as(str(save_path))
                            if save_path.stat().st_size > 0:
                                self._log("info", f"✓ 已保存PDF: {save_path.name} ({save_path.stat().st_size / 1024:.1f} KB)")
                                try:
                                    await popup.close()
                                except Exception:
                                    pass
                                return True, f"已保存: {save_path.name}"
                except Exception:
                    pass
                try:
                    await popup.close()
                except Exception:
                    pass

            if wait_result == "manual":
                try:
                    saved_files = [save_path] if save_path.exists() and save_path.stat().st_size > 0 else []
                    if saved_files:
                        f = saved_files[0]
                        self._log("info", f"✓ 已确认PDF保存: {f.name} ({f.stat().st_size / 1024:.1f} KB)")
                        return True, f"已保存: {f.name}"
                except Exception:
                    pass
                self._log("info", "已收到手动确认，继续下一位")
                return True, "已手动保存"

        # ⚠️ 注意：此处不再提供全局「下载」按钮兜底。
        # 全局搜索 button:has-text('下载') 会误点到「单位参保证明打印」等
        # 其它模块的下载按钮（v14/v15 已踩坑）。因此模块内定位失败时，
        # 直接进入诊断截图 + 手动兜底，绝不在错误模块上点击。
        if self._stopped():
            return False, "已停止"

        # 自动下载都未成功，截图保留现场以便排查
        try:
            await self._save_diag(f"download_fail_{employee.name}", full_page=True)
        except Exception:
            pass

        # ---------- Step 3: 手动兜底（引导用户手动点「下载」） ----------
        return await self._manual_fallback(
            employee, save_path,
            on_manual_print_confirm=on_manual_print_confirm,
            on_manual_print_request=on_manual_print_request,
        )

    async def _wait_download_result(
        self,
        dl_future,
        popup_future,
        on_manual_print_confirm=None,
        timeout: float = 60,
    ) -> str:
        """
        等待下载结果（可被用户确认按钮/停止请求中断）

        Returns:
            "download" 浏览器已开始下载文件（download 事件触发）
            "popup"    下载以新窗口打开
            "manual"   用户点击了「已保存PDF」确认按钮
            "stopped"  用户请求停止
            "timeout"  超时
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if dl_future.done():
                return "download"
            if popup_future.done():
                return "popup"
            if on_manual_print_confirm and on_manual_print_confirm():
                return "manual"
            if self._stopped():
                return "stopped"
            await asyncio.sleep(0.5)
        return "timeout"

    async def _click_download_in_dialog(self) -> bool:
        """
        点击页面内预览弹窗（el-dialog 等）中的「下载」按钮

        网厅常见交互: 点「下载」→ 页面弹出预览弹窗 → 弹窗内再点「下载」→ 触发文件下载
        """
        dialog_selectors = [
            ".el-dialog",
            ".el-drawer",
            ".el-message-box",
            ".modal-content",
            ".print-modal",
            ".print-preview",
        ]
        for dsel in dialog_selectors:
            try:
                dialogs = await self.page.query_selector_all(dsel)
            except Exception:
                continue
            for dlg in dialogs:
                try:
                    if not await dlg.is_visible():
                        continue
                except Exception:
                    continue
                button_selectors = [
                    "button:has-text('下载')",
                    "a:has-text('下载')",
                    ".el-button:has-text('下载')",
                    "[role='button']:has-text('下载')",
                    "[title='下载']",
                    "span:has-text('下载')",
                    "button:has-text('打印')",
                    "a:has-text('打印')",
                    "[role='button']:has-text('打印')",
                    ".el-button:has-text('打印')",
                    "[title='打印']",
                    "span:has-text('打印')",
                ]
                for bsel in button_selectors:
                    try:
                        btns = await dlg.query_selector_all(bsel)
                    except Exception:
                        continue
                    for b in btns:
                        try:
                            if await b.is_visible():
                                await b.click()
                                self._log("info", f"已点击弹窗内下载按钮 ({dsel})")
                                return True
                        except Exception:
                            continue
        return False

    async def _click_download_button(self) -> bool:
        """点击当前页面的「下载」按钮（优先），无则点「打印」按钮兜底（多选择器 + 支持 iframe）"""
        strategies = [
            # 真实流程：点「下载」按钮 → 浏览器直接下载
            "button:has-text('下载')",
            "a:has-text('下载')",
            ".el-button:has-text('下载')",
            "[role='button']:has-text('下载')",
            "[title='下载']",
            "[aria-label='下载']",
            ".el-icon-download",
            "span:has-text('下载')",
            "div[class*='btn']:has-text('下载')",
            # 兜底：部分平台仍用「打印」触发（打印=下载PDF）
            "button:has-text('打印')",
            "a:has-text('打印')",
            ".el-button:has-text('打印')",
            "[role='button']:has-text('打印')",
            "[title='打印']",
            "[aria-label='打印']",
            ".el-icon-printer",
            "span:has-text('打印')",
            "div[class*='btn']:has-text('打印')",
        ]
        for selector in strategies:
            found = await self._click_first_in_frames(selector)
            if found:
                self._log("info", f"已点击下载按钮 ({selector})")
                return True
        return False

    async def _click_download_button_on(self, page) -> bool:
        """在指定页面点击「下载」按钮（优先），无则点「打印」按钮兜底"""
        strategies = [
            "button:has-text('下载')",
            "a:has-text('下载')",
            ".el-button:has-text('下载')",
            "[role='button']:has-text('下载')",
            "[title='下载']",
            "[aria-label='下载']",
            "span:has-text('下载')",
            "div[class*='btn']:has-text('下载')",
            "button:has-text('打印')",
            "a:has-text('打印')",
            ".el-button:has-text('打印')",
            "[role='button']:has-text('打印')",
            "[title='打印']",
            "[aria-label='打印']",
            "span:has-text('打印')",
            "div[class*='btn']:has-text('打印')",
        ]
        for selector in strategies:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    if await el.is_visible():
                        await el.click()
                        return True
            except Exception:
                continue
        return False

    async def _click_first_in_frames(self, selector: str) -> bool:
        """在主页面及所有 iframe 中查找并点击第一个匹配的可见元素"""
        for frame in self.page.frames:
            try:
                elements = await frame.query_selector_all(selector)
                for el in elements:
                    try:
                        if await el.is_visible():
                            await el.click()
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    async def _find_list_module_rect(self):
        """
        定位「单位参保人员信息列表」模块卡片的位置（几何定位）。

        步骤：
        1. 在页面（含 iframe）中查找标题「单位参保人员信息列表」（先精确、后模糊）
        2. 从标题向上找最近的、包含表格（table/.el-table）的卡片容器
        3. 返回该容器的页面坐标矩形 {x, y, w, h}

        Returns:
            (frame, rect_dict) 或 None
        """
        js = """
        () => {
            const all = Array.from(document.querySelectorAll(
                'div, span, p, h1, h2, h3, h4, h5, section, .card, .el-card, .el-card__header, .card-title, .panel-title, .table-title'
            ));
            // 1. 精确匹配标题（元素自身直接文本 == 目标）
            let title = null;
            for (const el of all) {
                const direct = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => (n.textContent || '').trim())
                    .join('');
                if (direct === '单位参保人员信息列表') { title = el; break; }
            }
            // 2. 模糊匹配：innerText 包含目标，取子元素最少（最内层）者
            if (!title) {
                const cands = all.filter(el => (el.innerText || '').trim().includes('单位参保人员信息列表'));
                if (cands.length) {
                    cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
                    title = cands[0];
                }
            }
            if (!title) return null;
            // 3. 向上找包含表格的卡片容器，取其矩形
            let card = title;
            for (let i = 0; i < 6 && card; i++) {
                if (card.querySelector('table, .el-table, .data-table')) {
                    const r = card.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return { x: r.x, y: r.y, w: r.width, h: r.height };
                }
                card = card.parentElement;
            }
            // 4. 找不到表格容器，退而求其次用标题自身向下扩展区域
            const r2 = title.getBoundingClientRect();
            if (r2.width > 0) return { x: r2.x - 10, y: r2.y - 10, w: r2.width + 20, h: r2.height + 800 };
            return null;
        }
        """
        for frame in self.page.frames:
            try:
                rect = await frame.evaluate(js)
                if rect and rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
                    return frame, rect
            except Exception:
                continue
        return None

    async def _click_download_in_list_module(self, employee: Employee) -> bool:
        """
        精确点击「单位参保人员信息列表」模块内的下载/打印按钮。

        双重校验（缺一不可，杜绝点到其它模块的按钮）：
        1. 几何校验：按钮中心点必须落在「单位参保人员信息列表」模块卡片矩形内
        2. 文本校验：按钮的祖先容器文本必须包含「单位参保人员」

        点击优先级：
        - 优先点击所在行包含员工姓名 / 身份证号 / 身份证后6位的按钮（结果行内按钮）
        - 无法按行匹配时，点击模块内第一个可见下载按钮（逐人查询时结果通常只有一行）
        """
        # 先等待模块出现（渲染延迟保护）
        try:
            await self.page.wait_for_selector("text=单位参保人员信息列表", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

        found = await self._find_list_module_rect()
        if not found:
            self._log("warn", "未定位到「单位参保人员信息列表」模块卡片，回退按结果行匹配...")
            return await self._click_download_in_result_row(employee)

        frame, rect = found
        self._log("info", f"已定位「单位参保人员信息列表」模块区域: x={rect['x']:.0f} y={rect['y']:.0f} w={rect['w']:.0f} h={rect['h']:.0f}")

        btn_selectors = [
            # 下载相关
            "button:has-text('下载')",
            "a:has-text('下载')",
            ".el-button:has-text('下载')",
            "[role='button']:has-text('下载')",
            "[title='下载']",
            "[aria-label='下载']",
            ".el-icon-download",
            "span.el-icon-download",
            "i.el-icon-download",
            # 打印/导出相关（部分平台用打印/导出触发下载）
            "button:has-text('打印')",
            "a:has-text('打印')",
            ".el-button:has-text('打印')",
            "[role='button']:has-text('打印')",
            "[title='打印']",
            "[aria-label='打印']",
            ".el-icon-printer",
            "span.el-icon-printer",
            "i.el-icon-printer",
            "button:has-text('导出')",
            "a:has-text('导出')",
        ]

        candidates = []  # (element, row_text)
        for sel in btn_selectors:
            try:
                elems = await frame.query_selector_all(sel)
            except Exception:
                continue
            for el in elems:
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    continue
                # 几何校验：按钮中心点必须在模块卡片矩形内
                try:
                    box = await el.bounding_box()
                except Exception:
                    box = None
                if not box:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                if not (rect["x"] <= cx <= rect["x"] + rect["w"] and
                        rect["y"] <= cy <= rect["y"] + rect["h"]):
                    continue
                # 文本校验：祖先容器包含「单位参保人员」
                try:
                    in_module = await el.evaluate("""
                        el => {
                            let p = el.parentElement;
                            while (p && p !== document.body) {
                                const t = (p.innerText || '');
                                if (t.includes('单位参保人员')) return true;
                                p = p.parentElement;
                            }
                            return false;
                        }
                    """)
                except Exception:
                    in_module = False
                if not in_module:
                    continue
                # 取按钮所在行文本（用于按行匹配）
                try:
                    row_text = await el.evaluate("""
                        el => {
                            let p = el;
                            while (p) {
                                if (p.tagName === 'TR' || (p.classList && p.classList.contains('el-table__row'))) {
                                    return (p.innerText || '').trim();
                                }
                                p = p.parentElement;
                            }
                            return '';
                        }
                    """)
                except Exception:
                    row_text = ""
                candidates.append((el, row_text))

        if not candidates:
            self._log("warn", "「单位参保人员信息列表」模块内未找到可见的下载/打印按钮，回退按结果行匹配...")
            return await self._click_download_in_result_row(employee)

        # 优先点包含员工姓名/身份证号（或身份证后6位）所在行的按钮
        for el, row_text in candidates:
            if not row_text:
                continue
            hit = False
            if employee.name and employee.name in row_text:
                hit = True
            if employee.id_card and employee.id_card in row_text:
                hit = True
            if employee.id_card and len(employee.id_card) >= 10 and employee.id_card[-6:] in row_text:
                hit = True
            if hit:
                try:
                    await el.click()
                    self._log("info", f"已点击「单位参保人员信息列表」结果行内按钮（行匹配: {employee.name}）")
                    return True
                except Exception as e:
                    self._log("warn", f"点击行内下载按钮失败: {str(e)[:100]}")
                    continue

        # 逐人查询时结果通常只有一行：点击模块内第一个下载按钮
        el0, _ = candidates[0]
        try:
            await el0.click()
            self._log("info", "已点击「单位参保人员信息列表」模块内下载按钮（未按行匹配，逐人查询结果通常唯一）")
            return True
        except Exception as e:
            self._log("warn", f"点击模块内下载按钮失败: {str(e)[:100]}")
            return False

    async def _click_download_in_result_row(self, employee: Employee) -> bool:
        """
        点击「单位参保人员信息列表」查询结果行内的下载/打印按钮。

        先等待结果表格出现，再定位包含该员工姓名或身份证号的行，
        最后只在该行内点击下载/打印相关按钮，避免误点其它模块按钮。
        """
        # 等待结果行出现
        try:
            await self.page.wait_for_selector(
                ".el-table__row, tbody tr, tr.el-table__row, .el-table tbody tr",
                timeout=10000,
            )
        except Exception:
            pass

        row_selectors = [
            ".el-table__row",
            ".el-table tbody tr",
            "tbody tr",
            "tr[data-row]",
            ".table-row",
        ]

        # 遍历所有 frame，优先找包含员工姓名/身份证号的行
        for frame in self.page.frames:
            try:
                rows = []
                for sel in row_selectors:
                    rows = await frame.query_selector_all(sel)
                    if rows:
                        break

                for row in rows:
                    try:
                        is_visible = await row.is_visible()
                        if not is_visible:
                            continue
                    except Exception:
                        continue

                    try:
                        row_text = await row.inner_text()
                    except Exception:
                        continue

                    # 定位属于目标员工的行（姓名/身份证号/身份证后6位匹配其一）
                    row_match = False
                    if employee.name and employee.name in row_text:
                        row_match = True
                    if employee.id_card and employee.id_card in row_text:
                        row_match = True
                    if employee.id_card and len(employee.id_card) >= 10 and employee.id_card[-6:] in row_text:
                        row_match = True

                    # 逐人查询时结果通常只有一行：若该表格只有 1 行可见数据，视为目标行
                    if not row_match:
                        try:
                            visible_rows = []
                            for r in rows:
                                try:
                                    if await r.is_visible():
                                        visible_rows.append(r)
                                except Exception:
                                    pass
                            if len(visible_rows) == 1:
                                row_match = True
                        except Exception:
                            pass

                    if not row_match:
                        continue

                    # 在该行内查找下载/打印按钮或链接
                    action_selectors = [
                        "button",
                        "a",
                        ".el-button",
                        "[role='button']",
                        "span",
                        "i",
                        ".el-icon-download",
                        ".el-icon-printer",
                    ]
                    for asel in action_selectors:
                        try:
                            elems = await row.query_selector_all(asel)
                            for el in elems:
                                try:
                                    if not await el.is_visible():
                                        continue
                                except Exception:
                                    continue
                                try:
                                    txt = await el.inner_text()
                                except Exception:
                                    txt = ""
                                try:
                                    title = await el.get_attribute("title") or ""
                                    aria = await el.get_attribute("aria-label") or ""
                                    cls = await el.get_attribute("class") or ""
                                except Exception:
                                    title = aria = cls = ""

                                # 命中下载/打印相关文本或图标
                                keywords = ["下载", "打印", "导出", "down", "print", "export"]
                                matched = (
                                    any(k in txt for k in keywords) or
                                    any(k in title for k in keywords) or
                                    any(k in aria for k in keywords) or
                                    any(k in cls.lower() for k in ["download", "print", "export"]) or
                                    "el-icon-download" in cls or
                                    "el-icon-printer" in cls
                                )
                                if matched:
                                    try:
                                        await el.click()
                                        self._log("info", f"已点击结果行内操作按钮: txt={txt!r} title={title!r} class={cls!r}")
                                        return True
                                    except Exception:
                                        continue
                        except Exception:
                            continue

                    # 行内没有明显按钮，可能整行可点击；继续下一行
            except Exception:
                continue

        return False

    async def _manual_fallback(
        self,
        employee: Employee,
        save_path: Path,
        on_manual_print_confirm=None,
        on_manual_print_request=None,
    ) -> tuple[bool, str]:
        """手动兜底：未找到下载/打印按钮或自动流程失败，提示用户手动点「下载」"""
        self._log("warn", "未找到可用的「下载」按钮，需要手动操作")
        self._log("warn", "请在浏览器中手动点击「下载」（或点「打印」后打印机选「另存为PDF」）→ 保存到:")
        self._log("warn", f"    {save_path}")

        # 通知前端显示确认条
        if on_manual_print_request:
            try:
                on_manual_print_request(
                    f"请手动为 {employee.display} 下载PDF：点击页面「下载」按钮（若只有「打印」按钮，则点打印后打印机选「另存为PDF」）→ 保存为 {save_path.name}（目录: {save_path.parent}），然后点击「已保存PDF，继续下一位」"
                )
            except Exception:
                pass

        if not on_manual_print_confirm:
            return False, "自动下载失败，需手动下载PDF"

        # 等待用户确认（最多5分钟，可被停止请求中断）
        wait_start = time.time()
        while time.time() - wait_start < 300:
            if self._stopped():
                self._log("info", "已收到停止请求，中止等待手动保存")
                return False, "已停止"
            if on_manual_print_confirm():
                # 检查文件是否已保存（文件名以花名册姓名为准）
                try:
                    saved_files = [save_path] if save_path.exists() and save_path.stat().st_size > 0 else []
                    if saved_files:
                        f = saved_files[0]
                        self._log("info", f"✓ 已确认PDF保存: {f.name} ({f.stat().st_size / 1024:.1f} KB)")
                        return True, f"已保存: {f.name}"
                except Exception:
                    pass
                # 未找到文件也可能用户改名保存，尊重选择继续
                self._log("info", "已收到手动确认，继续下一位")
                return True, "已手动保存"
            await asyncio.sleep(0.5)

        return False, "等待手动保存超时（5分钟）"

    async def _fill_id_input(self, id_card: str) -> bool:
        """在证件号码输入框中输入身份证号（优先定位查询区域的输入框）"""
        # 尝试多种策略找到输入框，优先使用查询表单内的输入框
        strategies = [
            # 策略A：查询表单/搜索区域内包含"证件"的输入框
            ".search-form input[placeholder*='证件']",
            ".query-form input[placeholder*='证件']",
            ".el-form input[placeholder*='证件']",
            ".query-panel input[placeholder*='证件']",
            ".search-panel input[placeholder*='证件']",
            ".search-box input[placeholder*='证件']",
            "form input[placeholder*='证件']",
            ".el-card input[placeholder*='证件']",
            # 策略B：更宽泛的含"证件/身份证/号码"
            "input[placeholder*='证件']",
            "input[placeholder*='身份证']",
            "input[placeholder*='号码']",
            "input[placeholder*='请输入']",
            # 策略C：表格上方的搜索框（常见模式）
            ".el-input__inner[placeholder*='证件']",
            ".el-input__inner[placeholder*='输入']",
            # 策略D：通用输入框（兜底）
            "input[type='text']",
            ".el-input__inner",
        ]

        # 先尝试各 frame 中优先匹配"证件"的输入框
        for selector in strategies:
            for frame in self.page.frames:
                try:
                    elements = await frame.query_selector_all(selector)
                    for el in elements:
                        if await el.is_visible():
                            placeholder = await el.get_attribute("placeholder") or ""
                            # 优先选择包含"证件"的输入框
                            if "证件" in placeholder or "身份证" in placeholder or "号码" in placeholder:
                                await el.click()
                                await el.fill("")
                                await el.fill(id_card)
                                self._log("info", f"已输入证件号码: {id_card}")
                                return True
                except Exception:
                    continue

        # 兜底：使用任意可见输入框
        for selector in strategies:
            for frame in self.page.frames:
                try:
                    elements = await frame.query_selector_all(selector)
                    for el in elements:
                        if await el.is_visible():
                            await el.click()
                            await el.fill("")
                            await el.fill(id_card)
                            self._log("info", f"已输入证件号码: {id_card}")
                            return True
                except Exception:
                    continue

        return False

    async def _click_query_button(self) -> bool:
        """点击查询按钮（优先点击查询表单内的按钮，避免误点页面其他按钮）"""
        # 先尝试限定在查询/搜索区域内的按钮
        scoped_strategies = [
            ".search-form button:has-text('查询')",
            ".query-form button:has-text('查询')",
            ".el-form button:has-text('查询')",
            ".query-panel button:has-text('查询')",
            ".search-panel button:has-text('查询')",
            ".search-box button:has-text('查询')",
            "form button:has-text('查询')",
            ".el-card button:has-text('查询')",
            ".search-form .el-button:has-text('查询')",
            ".query-form .el-button:has-text('查询')",
        ]
        # 再尝试全页面按钮
        strategies = [
            "button:has-text('查询')",
            "button:has-text('搜索')",
            ".el-button:has-text('查询')",
            ".el-button:has-text('搜索')",
            "[role='button']:has-text('查询')",
            "text='查询'",
            "text='搜索'",
        ]

        all_strategies = scoped_strategies + strategies
        for selector in all_strategies:
            for frame in self.page.frames:
                try:
                    elements = await frame.query_selector_all(selector)
                    for el in elements:
                        if await el.is_visible():
                            await el.click()
                            self._log("info", "已点击查询按钮")
                            return True
                except Exception:
                    continue

        # 尝试按 Enter 键提交
        try:
            await self.page.keyboard.press("Enter")
            self._log("info", "已按 Enter 键提交查询")
            return True
        except Exception:
            pass

        return False

    async def _check_person_found(self, employee: Employee) -> tuple[bool, str]:
        """
        检查是否查询到人员

        Returns:
            (是否确认查到, 检测详情)
            注意: 此检测仅作提示用途，不再作为打印流程的硬性门槛
        """
        try:
            page_text = await self.page.inner_text("body", timeout=5000)

            # 检查"无数据"/"未找到"等提示
            not_found_markers = ["暂无数据", "无数据", "未找到", "没有找到", "查无此人", "无匹配"]
            found_markers = []
            for marker in not_found_markers:
                if marker in page_text:
                    found_markers.append(marker)

            # 检查是否有表格数据
            table_rows = await self.page.query_selector_all(
                ".el-table__row, .table-row, tr[data-row], tbody tr, .el-table tbody tr"
            )
            visible_rows = []
            for r in table_rows:
                try:
                    if await r.is_visible():
                        visible_rows.append(r)
                except Exception:
                    continue

            # 检查是否有下载/打印按钮出现（说明查到了，可进入打印）
            action_btns = await self.page.query_selector_all(
                "button:has-text('下载'), button:has-text('打印'), a:has-text('下载'), a:has-text('打印')"
            )
            visible_btns = []
            for btn in action_btns:
                try:
                    if await btn.is_visible():
                        visible_btns.append(btn)
                except Exception:
                    continue

            # 判断：只要满足任一"查到"特征即视为查到
            if employee.name in page_text:
                return True, f"页面包含员工姓名 {employee.name}"
            if visible_rows:
                return True, f"表格中有 {len(visible_rows)} 条数据行"
            if visible_btns:
                return True, f"页面出现 下载/打印 按钮（{len(visible_btns)} 个）"

            # 明确出现"无数据"标记且无任何行/按钮 → 视为未查到
            if found_markers:
                return False, f"页面出现提示: {'、'.join(found_markers)}"

            # 无法判断（既无查到特征也无无数据提示）→ 视为"无法确认"
            return False, "未发现明确的查询结果特征（页面结构可能不同）"

        except Exception as e:
            return False, f"检测异常: {str(e)[:80]}"

    def _sanitize_filename(self, filename: str, employee: Employee) -> str:
        """规范文件名：以花名册中的姓名命名（如 张三.pdf）"""
        # 始终以花名册姓名为准，与保存路径保持一致
        ext = Path(filename).suffix or ".pdf"
        filename = f"{employee.name}{ext}"

        # 清理非法字符
        invalid_chars = '<>:"/\\|?*'
        for c in invalid_chars:
            filename = filename.replace(c, "_")

        return filename

    async def batch_download(self, employees: list[Employee]) -> dict:
        """
        批量下载参保证明

        Args:
            employees: 员工列表

        Returns:
            结果统计 dict
        """
        total = len(employees)
        results = {"success": [], "failed": [], "skipped": []}

        self._log("info", f"开始批量下载，共 {total} 人")

        for i, emp in enumerate(employees, start=1):
            if self._stopped():
                self._log("info", "已收到停止请求，中止批量下载")
                break
            success, msg = await self.download_one(emp, i, total)

            if success:
                results["success"].append((emp, msg))
            elif msg and "未查询到" in msg:
                results["skipped"].append((emp, msg))
            else:
                results["failed"].append((emp, msg))

            # 下载间隔，避免请求过快
            if i < total:
                await asyncio.sleep(1.5)

        # 汇总
        self._log("info", "=" * 50)
        self._log("info", f"批量下载完成:")
        self._log("info", f"  成功: {len(results['success'])} 人")
        self._log("info", f"  跳过: {len(results['skipped'])} 人（未参保/已停保）")
        self._log("info", f"  失败: {len(results['failed'])} 人")
        self._log("info", f"  文件保存在: {self._download_dir}")

        if results["failed"]:
            self._log("info", "失败列表:")
            for emp, msg in results["failed"]:
                self._log("info", f"    {emp.display}: {msg}")

        if results["skipped"]:
            self._log("info", "跳过列表:")
            for emp, msg in results["skipped"]:
                self._log("info", f"    {emp.display}: {msg}")

        return results

    async def close(self):
        """关闭浏览器"""
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self._log("info", "浏览器已关闭")

    async def take_screenshot(self, name: str = "error"):
        """截图（用于错误诊断）"""
        if not self.page:
            return
        screenshot_path = config.LOG_DIR / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
            self._log("info", f"已截图: {screenshot_path}")
        except Exception:
            pass


def get_download_subdir():
    """返回按日期命名的子目录名"""
    return datetime.now().strftime("%Y-%m-%d")
