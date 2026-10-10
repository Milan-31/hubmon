@echo off
rem ---------------------------------------------------------------- hubmon
rem  Open the control panel in the default browser.
rem  If the monitor is not running yet, start it first (background, no console).
rem  Change PORT below if you moved the panel to another port.
rem ----------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8756"
set "URL=http://127.0.0.1:%PORT%/"

call :is_up
if "!UP!"=="1" goto open

if exist "dist\hubmon.exe" (
  start "" "dist\hubmon.exe" --no-browser
) else (
  start "" pythonw -m hubmon.main --no-browser
)

rem wait up to ~30s for the panel to start listening
for /l %%i in (1,1,30) do (
  call :is_up
  if "!UP!"=="1" goto open
  timeout /t 1 /nobreak >nul
)

:open
start "" "%URL%"
exit /b 0

rem ---------------------------------------------------------------- helpers
:is_up
set "UP=0"
for /f "delims=" %%a in ('netstat -ano -p TCP ^| findstr /i "LISTENING" ^| findstr ":%PORT%"') do set "UP=1"
exit /b 0