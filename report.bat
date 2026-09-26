@echo off
REM Print the shadow-book ledger. Safe to run while the poller is running.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% -m openclaw.report %*
echo.
pause
