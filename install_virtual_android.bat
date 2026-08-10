@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
set "PYTHON_ARGS="
if not exist "%PYTHON_EXE%" (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
  ) else (
    set "PYTHON_EXE=python"
  )
)

echo ============================================================
echo adbTaptap - install project-local Android Emulator runtime
echo Target: %CD%\runtime
echo ============================================================
"%PYTHON_EXE%" %PYTHON_ARGS% adb_manager\emulator_manager.py install

if errorlevel 1 (
  echo.
  echo Installation failed. Review the output above.
) else (
  echo.
  echo Installation completed.
)
pause
