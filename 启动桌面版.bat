@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "DESKTOP_RUNTIME=%CD%\runtime\desktop"
set "DESKTOP_PACKAGES=%CD%\runtime\desktop-packages"
if not exist "%DESKTOP_RUNTIME%" mkdir "%DESKTOP_RUNTIME%"
if not exist "%DESKTOP_PACKAGES%" mkdir "%DESKTOP_PACKAGES%"
if not exist "%DESKTOP_RUNTIME%\temp" mkdir "%DESKTOP_RUNTIME%\temp"
if not exist "%DESKTOP_RUNTIME%\pip-cache" mkdir "%DESKTOP_RUNTIME%\pip-cache"

set "TEMP=%DESKTOP_RUNTIME%\temp"
set "TMP=%DESKTOP_RUNTIME%\temp"
set "PIP_CACHE_DIR=%DESKTOP_RUNTIME%\pip-cache"
set "PYTHONPATH=%DESKTOP_PACKAGES%;%PYTHONPATH%"

set "PYTHON_EXE="
if defined PYTHON_EXECUTABLE if exist "%PYTHON_EXECUTABLE%" set "PYTHON_EXE=%PYTHON_EXECUTABLE%"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not defined PYTHON_EXE (
  where py >nul 2>nul
  if not errorlevel 1 for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)"') do set "PYTHON_EXE=%%I"
)
if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 for /f "delims=" %%I in ('python -c "import sys;print(sys.executable)"') do set "PYTHON_EXE=%%I"
)

if not defined PYTHON_EXE (
  echo [FAIL] No Python 3 installation was found.
  echo Please install Python 3 or create the project .venv first.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import sys;sys.path.insert(0,r'%DESKTOP_PACKAGES%');import webview;assert hasattr(webview,'create_window')" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing desktop WebView dependencies into project runtime...
  "%PYTHON_EXE%" -m pip install --disable-pip-version-check --upgrade --target "%DESKTOP_PACKAGES%" -r desktop-requirements.txt
  if errorlevel 1 (
    echo [FAIL] Desktop dependency installation failed.
    pause
    exit /b 1
  )
)

set "PYTHONW_EXE=%PYTHON_EXE:python.exe=pythonw.exe%"
if not exist "%PYTHONW_EXE%" set "PYTHONW_EXE=%PYTHON_EXE%"
start "ADB Device Manager" "%PYTHONW_EXE%" "%CD%\desktop_app.py"
exit /b 0
