@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" (
  echo.
  echo Startup failed. Check the error above.
  pause
)
exit /b %exitCode%
