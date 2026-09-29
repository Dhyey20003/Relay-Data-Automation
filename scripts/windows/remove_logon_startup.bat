@echo off
cd /d "%~dp0"
if exist RelayEmployeeHub.exe ( RelayEmployeeHub.exe --remove-startup & exit /b )
cd /d "%~dp0..\.."
if exist dist\RelayEmployeeHub\RelayEmployeeHub.exe ( dist\RelayEmployeeHub\RelayEmployeeHub.exe --remove-startup & exit /b )
.venv\Scripts\python.exe -m relay_hub --remove-startup
