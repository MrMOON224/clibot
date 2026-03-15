@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found. Run setup_windows.ps1 first.
    pause
    exit /b 1
)

:: Force UTF-8 so OpenCode's unicode output doesn't crash
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo  Starting OpenCode Telegram Agent...
echo  Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe bot.py

pause
