@echo off
rem Installs Relay Employee Hub from source into a local virtual environment.
rem Requires Python 3.12 (64-bit) from python.org. Downloads the pinned
rem dependencies from PyPI once; after that the application runs offline.
setlocal
cd /d "%~dp0..\.."
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found. Install Python 3.12 64-bit from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
py -3.12 -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || (echo Dependency installation failed. & pause & exit /b 1)
echo.
echo Installed. Start the application with scripts\windows\run_from_source.bat
pause
