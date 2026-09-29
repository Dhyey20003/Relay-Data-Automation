@echo off
rem Builds the packaged Windows application: dist\RelayEmployeeHub\RelayEmployeeHub.exe
rem The build folder is self-contained and runs offline (no Python needed on the target PC).
setlocal
cd /d "%~dp0..\.."
where py >nul 2>nul || (echo Install Python 3.12 64-bit first. & pause & exit /b 1)
if not exist .venv-build\Scripts\python.exe py -3.12 -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install --upgrade pip
.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt || (echo Dependency installation failed. & pause & exit /b 1)
.venv-build\Scripts\pyinstaller.exe --noconfirm --clean packaging\relay_hub.spec || (echo Build failed. & pause & exit /b 1)
copy /Y scripts\windows\create_desktop_shortcut.bat dist\RelayEmployeeHub\ >nul
copy /Y scripts\windows\install_logon_startup.bat dist\RelayEmployeeHub\ >nul
copy /Y scripts\windows\remove_logon_startup.bat dist\RelayEmployeeHub\ >nul
copy /Y docs\OPERATOR_GUIDE.md dist\RelayEmployeeHub\ >nul
copy /Y THIRD_PARTY_NOTICES.md dist\RelayEmployeeHub\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\RelayEmployeeHub -DestinationPath dist\RelayEmployeeHub-windows.zip"
echo.
echo Built: dist\RelayEmployeeHub\RelayEmployeeHub.exe
echo Zip for copying to the office computer: dist\RelayEmployeeHub-windows.zip
pause
