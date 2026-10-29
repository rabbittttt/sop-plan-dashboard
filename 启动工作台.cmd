@echo off
setlocal
chcp 65001 >nul
title SOP计划工作台
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-sop.ps1" %*
set "SOP_EXIT_CODE=%ERRORLEVEL%"
if not "%SOP_EXIT_CODE%"=="0" (
    echo.
    echo SOP workbench could not start. Please see the error above.
    if /I not "%~1"=="-NoBrowser" pause
)
exit /b %SOP_EXIT_CODE%
