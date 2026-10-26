@echo off
chcp 65001 >nul
setlocal EnableExtensions

set VENV=D:\aqm-dev\venv\Scripts
set PORT=8000
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo  ============================================================
echo   aqm Web UI
echo  ============================================================
echo.
echo   Web UI : http://localhost:%PORT%
echo.

echo  Starting web server in new window...
start "aqm-dashboard" "%VENV%\aqm.exe" serve --port %PORT% --host 127.0.0.1

echo  Waiting for server to start (8 seconds)...
ping -n 9 127.0.0.1 >nul

echo  Opening browser...
start http://localhost:%PORT%

echo.
echo  ============================================================
echo   Server running at http://localhost:%PORT%
echo   Close the aqm-dashboard window to stop.
echo  ============================================================
echo.
pause
