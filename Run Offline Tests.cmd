@echo off
cd /d "%~dp0"
python -I -B run_tests.py
pause
