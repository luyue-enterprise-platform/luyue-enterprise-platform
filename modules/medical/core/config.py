# -*- coding: utf-8 -*-
"""
医保参保证明批量下载模块 — 配置文件
（移植自独立工具「医保证明下载工具」，集成进鲁岳企业服务综合智能平台）

路径约定（与平台其他模块一致）：
- 下载文件 / 上传文件 / 日志 -> core.paths.data_dir()（安装到 Program Files 时自动重定向 %APPDATA%）
- 模板等只读资源 -> core.paths.resource_dir()（打包后位于 _MEIPASS）
- Playwright 浏览器引擎 -> exe 旁「浏览器引擎」目录（由安装器释放，510MB 不进 EXE）
"""

import os
import sys
from pathlib import Path

from core.paths import data_dir, resource_dir

# ==================== 平台配置 ====================

# 陕西省医保公共服务平台网址
PORTAL_URL = "https://zwfw.shaanxi.gov.cn/ggfw/hallEnter/#/Index"

# 参保证明查询打印页面 URL（用户手动登录后，程序自动导航到此页面开始批量下载）
TARGET_URL = "https://zwfw.shaanxi.gov.cn/ggfw/hallUnit/#/staff-insu-print"

# 浏览器超时时间（毫秒）
NAVIGATION_TIMEOUT = 30000      # 页面导航超时
DOWNLOAD_TIMEOUT = 60000        # 下载超时
ELEMENT_TIMEOUT = 15000         # 元素查找超时

# ==================== 路径配置 ====================

IS_FROZEN = getattr(sys, "frozen", False)

DATA_DIR = Path(data_dir())
RESOURCE_DIR = Path(resource_dir())

# 下载文件：数据目录/outputs/medical/日期/（按日期子文件夹组织）
DOWNLOAD_DIR = DATA_DIR / "outputs" / "medical"

# 上传的员工名单临时存放
UPLOAD_DIR = DATA_DIR / "uploads" / "medical"

# 运行日志（含诊断截图）
LOG_DIR = DATA_DIR / "logs" / "medical"

# 模板目录：打包后从 _MEIPASS/modules/medical/templates 读取 build.spec 打入的模板
TEMPLATE_DIR = RESOURCE_DIR / "modules" / "medical" / "templates"


# ==================== Playwright 浏览器引擎路径 ====================

def _setup_browser_engine():
    """
    定位 Playwright 浏览器引擎目录（chromium-1148 等，约 510MB）。

    引擎体积过大不打入 EXE，由安装器释放到安装目录旁的「浏览器引擎」目录。
    按候选顺序查找，找到后设置 PLAYWRIGHT_BROWSERS_PATH 环境变量。

    Returns:
        引擎目录 Path，未找到返回 None（Playwright 将回退到默认路径）
    """
    candidates = []
    if IS_FROZEN:
        exe_dir = Path(sys.executable).parent.resolve()
        candidates.append(exe_dir / "浏览器引擎")
        candidates.append(exe_dir.parent / "浏览器引擎")
    else:
        # 开发模式：项目根目录旁 / 独立工具安装目录
        project_root = Path(__file__).resolve().parents[3]
        candidates.append(project_root / "浏览器引擎")
        candidates.append(project_root.parent / "浏览器引擎")
        candidates.append(Path(r"D:\医保证明下载工具\浏览器引擎"))

    for c in candidates:
        try:
            if c.is_dir() and any(c.iterdir()):
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(c))
                return c
        except Exception:
            continue
    return None


BROWSER_ENGINE_DIR = _setup_browser_engine()


# 下载文件存放目录（按日期子文件夹组织）
def get_download_subdir():
    """返回按日期命名的下载子目录"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")

# ==================== Excel 列名映射 ====================

# 支持多种常见列名写法，按优先级匹配
EXCEL_COLUMN_MAP = {
    "name": ["姓名", "名字", "员工姓名", "人员姓名", "name"],
    "id_card": ["身份证号", "证件号码", "身份证号码", "身份证", "证件号", "id_card"],
    "phone": ["手机号", "电话", "联系电话", "手机号码", "phone"],
    "remark": ["备注", "说明", "remark"],
}

# ==================== 日志配置 ====================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ==================== 初始化目录 ====================

for d in [DOWNLOAD_DIR, UPLOAD_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)
