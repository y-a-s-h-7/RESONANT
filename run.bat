@echo off
setlocal
rem Launcher for Resonant — voice -> clean text, fully local.
rem Starts Resonant in the background (tray icon near the taskbar clock;
rem the dashboard window can be re-opened from the tray at any time).
rem Quit anytime from the tray menu: Exit (or press Ctrl+Shift+Scroll Lock).

rem Prioritize local virtual environment (.venv) if present
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  set "PYW=%~dp0.venv\Scripts\pythonw.exe"
  goto :launch
)

set "PYW=pythonw.exe"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found on PATH or in .venv.
  echo Please install Python 3.11 to 3.14 or create a virtual environment in .venv.
  echo Refer to README.md for installation instructions.
  pause
  exit /b 1
)

:launch
start "" /b "%PYW%" "%~dp0resonant.py" %*
exit /b 0
