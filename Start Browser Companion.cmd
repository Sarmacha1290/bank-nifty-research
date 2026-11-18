@echo off
cd /d "%~dp0"
python -B -m banknifty.browser_companion
if errorlevel 1 pause
