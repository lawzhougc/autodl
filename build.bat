@echo off
chcp 65001 >nul
echo 正在执行飞牛 NAS autodl 自动构建与打包...

set PY_EXE=python
python --version >nul 2>&1
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\rion-wechat-cli\venv\Scripts\python.exe" (
        set PY_EXE="%LOCALAPPDATA%\rion-wechat-cli\venv\Scripts\python.exe"
    ) else (
        where py >nul 2>&1 && set PY_EXE=py
    )
)

%PY_EXE% fpk\build_fpk.py
echo.
pause
