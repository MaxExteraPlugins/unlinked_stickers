@echo off
setlocal
chcp 65001 >nul 2>&1
if exist "%~dp0..\..\tools\build_core.bat" (
    call "%~dp0..\..\tools\build_core.bat" "%~dp0"
) else (
    call "%~dp0bin\tools\build_core.bat" "%~dp0"
)
endlocal
pause
exit /b %ERRORLEVEL%
