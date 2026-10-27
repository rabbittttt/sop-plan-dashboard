@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0停止看板.ps1"
set "SOP_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%SOP_EXIT_CODE%"=="0" echo Failed to stop the SOP dashboard. Please see the error above.
if /I not "%~1"=="-NoPause" pause
exit /b %SOP_EXIT_CODE%
