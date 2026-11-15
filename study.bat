@echo off
REM Sprint 0.2.2 to 0.2.6: replay the harvested corpus through the live
REM pre-filter and the shadow book's entry rules, and print the gate readout.
REM The first run downloads price history for every ticker (about ten
REM minutes); after that it is cached and a rerun takes seconds.
REM The full report is also written to data\research\eventstudy_report.txt
REM and the per-trade table to data\research\eventstudy_trades.csv.
REM Arguments pass straight through, for example:
REM     study.bat --start 2025-11-01 --end 2026-10-31
REM     study.bat --no-dedupe
REM     study.bat --refresh-prices

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% -m openclaw.eventstudy %*
echo.
pause
