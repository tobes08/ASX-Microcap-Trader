@echo off
REM One-click verification: installs dependencies, then runs the self-test.
REM Everything is written to verify_output.txt as well as this window.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set "OUT=%~dp0verify_output.txt"

title OpenClaw verification

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

echo OpenClaw verification > "%OUT%"
echo ==================== >> "%OUT%"
echo. >> "%OUT%"

echo [1/3] Python version...
%PY% --version >> "%OUT%" 2>&1

echo [2/3] Installing dependencies (this can take a minute)...
echo. >> "%OUT%"
echo --- pip install --- >> "%OUT%"
%PY% -m pip install -r requirements.txt >> "%OUT%" 2>&1

echo [3/3] Running self-test...
echo. >> "%OUT%"
echo --- selftest --- >> "%OUT%"
%PY% selftest.py >> "%OUT%" 2>&1

echo.
type "%OUT%"
echo.
echo ---------------------------------------------------------------
echo Output also saved to verify_output.txt
echo ---------------------------------------------------------------
pause
