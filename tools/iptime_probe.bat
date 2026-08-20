@echo off
cd /d "%~dp0.."
python tools\iptime_probe.py
if errorlevel 1 pause
