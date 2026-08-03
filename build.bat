@echo off
chcp 65001 >nul
set "PYTHON=C:\Users\20466\.workbuddy\binaries\python\envs\luyue_pack_py311\Scripts\python.exe"
set "DIST_DIR=dist"
set "NAME=鲁岳企业服务_综合智能平台"

echo ============================================
echo   鲁岳企业服务·综合智能平台 — 打包脚本
echo ============================================
echo.

if not exist "%PYTHON%" (
    echo [ERROR] Python 3.11 打包环境不存在: %PYTHON%
    pause
    exit /b 1
)

echo [INFO] 使用 Python: %PYTHON%
echo.

echo [1/2] 清理旧构建...
if exist build rmdir /s /q build
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
echo [OK] 清理完成

echo.
echo [2/2] 开始打包...
"%PYTHON%" -m PyInstaller --noconfirm build.spec

if %ERRORLEVEL% NEQ 0 (
    echo [FAIL] 打包失败
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包完成！
echo   输出: %DIST_DIR%\%NAME%.exe
echo ============================================
pause
