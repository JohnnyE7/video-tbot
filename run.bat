@echo off
rem Runs the bot and restarts it after a crash.
cd /d "%~dp0"
:loop
.venv\Scripts\python.exe -m bot
ping -n 6 127.0.0.1 >nul
goto loop
