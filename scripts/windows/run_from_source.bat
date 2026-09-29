@echo off
rem Starts Relay Employee Hub from source and opens it in the browser.
cd /d "%~dp0..\.."
if not exist .venv\Scripts\pythonw.exe (
  echo Run scripts\windows\setup_from_source.bat first.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m relay_hub %*
