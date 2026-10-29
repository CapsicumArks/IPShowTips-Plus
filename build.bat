@echo off
chcp 65001 >nul
title IPShowTips-Plus 打包脚本
cd /d "%~dp0"

echo [1/3] 检查 PyInstaller...
python -c "import PyInstaller" 2>nul
if %errorlevel% neq 0 (
    echo     未安装，正在安装 PyInstaller...
    pip install pyinstaller
)

echo [2/3] 开始打包（可能需要 1-2 分钟）...
python -m PyInstaller --noconfirm --onefile --windowed ^
    --name "IPShowTips-Plus" ^
    --icon "assets\icon.ico" ^
    --hidden-import "pystray._win32" ^
    ip_widget.py

if %errorlevel% neq 0 (
    echo [错误] 打包失败，请检查上方报错信息
    pause
    exit /b 1
)

echo [3/3] 打包完成
echo     输出: %cd%\dist\IPShowTips-Plus.exe
pause
