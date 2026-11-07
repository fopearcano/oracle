@echo off
rem Double-click to start Oracle on Windows.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 oracle.py %*
) else (
  python oracle.py %*
)
if errorlevel 1 pause
