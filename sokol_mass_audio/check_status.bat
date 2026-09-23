@echo off
cd /d "%~dp0"
title Sokol rev.5 - Status
py -3 "%~dp0sokol_mass_audio.py" status
echo.
echo Exit code: %ERRORLEVEL%
pause
