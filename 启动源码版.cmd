@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动まあ丸源码版启动器...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" maamaru_launcher.py
) else (
  python maamaru_launcher.py
)
