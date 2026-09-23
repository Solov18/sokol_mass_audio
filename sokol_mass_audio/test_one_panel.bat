@echo off
cd /d "%~dp0"
title Sokol rev.5 - Test one panel
set /p PANEL_IP=Enter test panel IP: 
py -3 "%~dp0sokol_mass_audio.py" test --only "%PANEL_IP%"
echo.
echo Exit code: %ERRORLEVEL%
pause
