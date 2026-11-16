@echo off
rem Starts PW Mount Control from the repo (dev venv + web/.output/public), no console window.
cd /d "%~dp0..\.."
start "" "%~dp0..\..\.venv\Scripts\pythonw.exe" -m launcher
