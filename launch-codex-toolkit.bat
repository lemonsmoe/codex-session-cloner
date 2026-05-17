@echo off
setlocal
cd /d "%~dp0"
call "%~dp0codex-session-toolkit.cmd" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Codex Session Toolkit exited with code %EXIT_CODE%.
  echo Please screenshot the error above before closing this window.
  pause
)
exit /b %EXIT_CODE%
