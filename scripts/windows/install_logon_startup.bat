@echo off
rem Optional: start Relay Employee Hub in the background when this Windows user signs in.
rem Scheduled tasks run only while the computer is on and this user is signed in.
cd /d "%~dp0"
if exist RelayEmployeeHub.exe ( RelayEmployeeHub.exe --install-startup & exit /b )
cd /d "%~dp0..\.."
if exist dist\RelayEmployeeHub\RelayEmployeeHub.exe ( dist\RelayEmployeeHub\RelayEmployeeHub.exe --install-startup & exit /b )
.venv\Scripts\python.exe -m relay_hub --install-startup
