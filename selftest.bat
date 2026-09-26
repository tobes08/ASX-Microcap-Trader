@echo off
REM Verify the plumbing: feed reachable, prices working, ledger arithmetic
REM correct. Writes to a scratch database, never touches the real one.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% selftest.py
echo.
pause
