@echo off
cd /d "%~dp0"
title Sokol rev.5 - Prepare and schedule
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_activation.ps1"
echo.
echo Exit code: %ERRORLEVEL%
pause
