@echo off
REM One-time dependency install.

setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

echo Installing dependencies...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt

echo.
echo Done. Next: run selftest.bat
pause
