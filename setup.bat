@echo off
cd /d "%~dp0"
title KK-Agent setup
echo ============================================
echo   KK-Agent dependency install (CN mirror)
echo ============================================
echo.

rem ---- 1. check python ----
python --version > nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo.
  echo Please install Python 3.10+ first:
  echo   1. open https://www.python.org/downloads/
  echo   2. run the installer
  echo   3. CHECK "Add Python to PATH" during install
  echo   4. re-run this script
  echo.
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [1/3] Python %PYVER% found

rem ---- 2. install deps (tsinghua mirror, fallback aliyun) ----
echo [2/3] installing dependencies (tsinghua mirror)...
python -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check --quiet
if errorlevel 1 (
  echo tsinghua mirror failed, retry with aliyun mirror...
  python -m pip install -r "%~dp0requirements.txt" -i https://mirrors.aliyun.com/pypi/simple --disable-pip-version-check --quiet
)
if errorlevel 1 (
  echo [ERROR] install failed. check network and retry, or run manually:
  echo   python -m pip install -r requirements.txt
  pause
  exit /b 1
)
echo       dependencies installed

rem ---- 3. verify ----
python -c "import httpx, fastapi, uvicorn, yaml" 2> nul
if errorlevel 1 (
  echo [ERROR] verification failed. try manual install.
  pause
  exit /b 1
)
echo [3/3] verification passed

echo.
echo ============================================
echo   DONE! Next: double-click KK-Agent.bat
echo ============================================
echo.
pause
