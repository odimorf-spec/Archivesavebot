@echo off
chcp 6501 >nul
cd /d "%~dp0"
pip install -r requirements.txt
python userbot.py
pause
