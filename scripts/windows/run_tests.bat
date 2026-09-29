@echo off
rem Runs the automated test suite (synthetic data only; no email is sent).
cd /d "%~dp0..\.."
if not exist .venv-test\Scripts\python.exe py -3.12 -m venv .venv-test
.venv-test\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv-test\Scripts\python.exe -m pytest -q
pause
