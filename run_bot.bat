@echo off
chcp 6501 >nul
cd /d "%~dp0"
pip install -r requirements.txt
python bot.py
pause
