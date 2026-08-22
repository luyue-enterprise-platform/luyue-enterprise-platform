# -*- mode: python ; coding: utf-8 -*-
"""
鲁岳企业服务·重点群体涉税申报项目资料处理综合智能平台 — PyInstaller 打包配置
整合社保批量统计 + 劳动合同整理两个子模块
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs, copy_metadata

PROJECT_DIR = os.path.dirname(os.path.abspath(SPECPATH))
NAME = '鲁岳企业服务_综合智能平台'
ICON_FILE = os.path.join(PROJECT_DIR, 'static', 'assets', 'logo.ico')

# ---- 收集 rapidocr-onnxruntime ----
rapidocr_hidden = collect_submodules('rapidocr_onnxruntime')
rapidocr_data = collect_data_files('rapidocr_onnxruntime')

# ---- 收集 onnxruntime 原生 DLL ----
onnxruntime_binaries = collect_dynamic_libs('onnxruntime')

# ---- 收集 VC++ 运行时 DLL ----
def collect_vc_runtime_dlls():
    import glob
    dlls = []
    for name in ['msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']:
        full = os.path.join(r'C:\Windows\System32', name)
        if os.path.isfile(full):
            dlls.append((full, '.'))
    return dlls

vc_runtime_binaries = collect_vc_runtime_dlls()

# ---- 收集 openpyxl ----
openpyxl_hidden = collect_submodules('openpyxl')

# ---- 收集 pdf2docx ----
pdf2docx_hidden = collect_submodules('pdf2docx')
pdf2docx_data = collect_data_files('pdf2docx')
docx_hidden = collect_submodules('docx')
docx_data = collect_data_files('docx')

# ---- pywebview ----
webview_hidden = collect_submodules('webview')
webview_data = collect_data_files('webview')
clr_loader_data = collect_data_files('clr_loader')
pythonnet_data = collect_data_files('pythonnet')

# ---- pywin32 (Word/Excel COM自动化) ----
pywin32_hidden = collect_submodules('win32com')
pywin32_data = collect_data_files('win32com')

# ---- playwright（医保参保证明批量下载模块，浏览器自动化） ----
# node driver（约70MB）打进 EXE 数据区（运行时解压到 _MEIPASS/playwright/driver）；
# Chromium 浏览器引擎（约510MB）不进 EXE，由安装器释放到安装目录旁的「浏览器引擎」目录，
# 运行时通过 PLAYWRIGHT_BROWSERS_PATH 环境变量指向（见 modules/medical/core/config.py）
playwright_hidden = collect_submodules('playwright') + [
    'playwright._impl._driver',
    'playwright.async_api',
]
_playwright_driver = os.path.join(PROJECT_DIR, 'venv', 'Lib', 'site-packages', 'playwright', 'driver')
playwright_data = [(_playwright_driver, 'playwright/driver')] if os.path.isdir(_playwright_driver) else []

# ---- 数据文件：模板 + 静态资源 ----
all_datas = [
    # 共享模板（login.html, register.html）
    ('templates', 'templates'),
    # 共享静态资源（含 logo.ico / logo.png / logo.jpg）
    ('static', 'static'),
    # 门户
    ('portal', 'portal'),
    # 保险模块
    ('modules/insurance/templates', 'modules/insurance/templates'),
    ('modules/insurance/static', 'modules/insurance/static'),
    # 合同模块
    ('modules/contract/templates', 'modules/contract/templates'),
    ('modules/contract/static', 'modules/contract/static'),
    # PDF转Word模块
    ('modules/pdf2word/templates', 'modules/pdf2word/templates'),
    ('modules/pdf2word/static', 'modules/pdf2word/static'),
    # 医保参保证明下载模块（templates 含 员工名单模板.xlsx）
    ('modules/medical/templates', 'modules/medical/templates'),
    ('modules/medical/static', 'modules/medical/static'),
    # 版本信息（用于远程更新检查）
    ('version.json', '.'),
    # 远程认证配置（内嵌回退：外部 auth_config.json 丢失时使用）
    ('auth_config.json', '.'),
] + rapidocr_data + webview_data + clr_loader_data + pythonnet_data + pdf2docx_data + docx_data + pywin32_data + playwright_data

a = Analysis(
    ['launcher.py'],
    pathex=[PROJECT_DIR],
    binaries=onnxruntime_binaries + vc_runtime_binaries,
    datas=all_datas,
    hiddenimports=[
        # Flask
        'flask', 'flask.json', 'werkzeug', 'jinja2', 'jinja2.ext',
        'markupsafe', 'itsdangerous', 'click',
        # OCR
        'rapidocr_onnxruntime',
        # PDF
        'fitz',
        'pdf2docx',
        'docx', 'docx.opc', 'docx.oxml', 'docx.parts', 'docx.text',
        # Excel
        'openpyxl', 'openpyxl.cell', 'openpyxl.styles', 'openpyxl.utils',
        'openpyxl.worksheet', 'openpyxl.reader', 'openpyxl.writer',
        'openpyxl.drawing', 'openpyxl.chart', 'openpyxl.workbook',
        # 图片
        'PIL', 'PIL.Image',
        'cv2',
        # pywebview
        'webview', 'webview.platforms', 'webview.platforms.edgechromium',
        'webview.platforms.cef', 'webview.js', 'webview.guilib',
        'pythonnet', 'clr', 'clr_loader', 'bottle', 'proxy_tools',
        'typing_extensions',
        # 标准库
        'json', 'csv', 'sqlite3', 'hashlib', 'secrets', 'uuid',
        'logging', 'ctypes', 'zipfile', 'shutil',
        # tkinter（用于系统原生文件夹选择对话框）
        'tkinter', 'tkinter.filedialog', 'tkinter.commondialog',
        # pywin32（用于Word/Excel转PDF）
        'win32com', 'win32com.client', 'pythoncom', 'pywintypes',
        # playwright（医保参保证明下载模块）
        'playwright.async_api', 'playwright._impl._driver', 'greenlet',
    ] + rapidocr_hidden + openpyxl_hidden + webview_hidden + pdf2docx_hidden + docx_hidden + pywin32_hidden + playwright_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy.random._examples', 'pandas.tests',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE if os.path.isfile(ICON_FILE) else None,
)
