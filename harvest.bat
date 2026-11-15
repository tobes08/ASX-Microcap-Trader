@echo off
REM Sprint 0.2.1: pull historical announcements into data\research\history.sqlite.
REM Resumable: run it again and it only fetches what is missing.
REM With no arguments it harvests the last 24 months, price-sensitive only.
REM Arguments pass straight through, for example:
REM     harvest.bat --months 12
REM     harvest.bat --start 2025-07-01 --end 2025-12-31
REM     harvest.bat --status
REM The corpus is for private, personal use. Do not share or commit it.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

if "%~1"=="" (
    %PY% -m openclaw.harvest --months 24 --price-sensitive-only
) else (
    %PY% -m openclaw.harvest %*
)
echo.
pause
