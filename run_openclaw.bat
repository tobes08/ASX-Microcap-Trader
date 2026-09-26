@echo off
REM OpenClaw - ASX poller + pre-filter + forward paper simulation.
REM Double-click to start. Ctrl+C to stop cleanly.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

title OpenClaw shadow book

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

echo Starting OpenClaw. Leave this window open. Ctrl+C stops it cleanly.
echo.
%PY% run_openclaw.py

echo.
echo OpenClaw has exited.
pause
