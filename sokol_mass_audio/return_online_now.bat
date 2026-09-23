@echo off
cd /d "%~dp0"
title Sokol rev.5 - Return Online
py -3 "%~dp0sokol_mass_audio.py" online
echo.
echo Exit code: %ERRORLEVEL%
pause
