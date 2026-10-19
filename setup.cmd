@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\manage.ps1" -Action Setup %*
set "PYED_EXIT=%ERRORLEVEL%"
if not "%PYED_EXIT%"=="0" pause
exit /b %PYED_EXIT%
