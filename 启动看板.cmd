@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-sop.ps1" %*
set "SOP_EXIT_CODE=%ERRORLEVEL%"
if not "%SOP_EXIT_CODE%"=="0" (
    echo.
    echo SOP dashboard did not start. Please see the error above.
    if /I not "%~1"=="-NoBrowser" pause
)
exit /b %SOP_EXIT_CODE%
