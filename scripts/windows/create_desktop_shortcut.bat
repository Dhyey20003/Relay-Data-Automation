@echo off
rem Creates a "Relay Employee Hub" shortcut on this user's desktop.
cd /d "%~dp0"
if exist RelayEmployeeHub.exe ( RelayEmployeeHub.exe --create-desktop-shortcut & exit /b )
cd /d "%~dp0..\.."
if exist dist\RelayEmployeeHub\RelayEmployeeHub.exe ( dist\RelayEmployeeHub\RelayEmployeeHub.exe --create-desktop-shortcut & exit /b )
.venv\Scripts\python.exe -m relay_hub --create-desktop-shortcut
