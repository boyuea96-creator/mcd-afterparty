@echo off
chcp 65001 >nul
REM 双击这个文件就能跑起来：起服务 + 自动打开浏览器。
REM 停止：关掉这个黑色窗口，或者按 Ctrl+C。
cd /d "%~dp0"

set PORT=%1
if "%PORT%"=="" set PORT=8765

where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 python。装一个再来：https://www.python.org/downloads/
  echo 安装时记得勾上 "Add Python to PATH"。
  pause
  exit /b 1
)

start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:%PORT%"
python server.py %PORT%
pause
