@echo off
REM Offline tests. Fabricated data only: no network, and nothing that touches
REM the live ledger or the research corpus.

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% -m unittest discover -s tests -t .
echo.
pause
