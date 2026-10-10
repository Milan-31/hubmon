@echo off
rem hubmon - live PC status dashboard for the jadot V1PRO hub screen
cd /d "%~dp0"
python -u -m hubmon.main %*
pause