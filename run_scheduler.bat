@echo off
cd /d "%~dp0"
python scheduler.py --config config.ini
pause
