@echo off
rem ============================================================
rem  WebPilot stop (keeps data volumes)
rem  docker compose down removes the 5 containers but KEEPS the
rem  pgdata / hfcache volumes and the runs/ folder - your data survives.
rem  (Nuke everything including data: docker compose down -v, careful!)
rem  NOTE: keep this file pure ASCII (see start.bat header for why).
rem ============================================================
setlocal EnableExtensions
title WebPilot stop
cd /d "%~dp0"

echo.
echo  Stopping WebPilot (data is kept)...
docker compose down
if errorlevel 1 (
    echo.
    echo  [ERROR] docker compose down failed. If Docker is not running at all,
    echo          the stack was not up in the first place - safe to ignore.
    pause
    exit /b 1
)
echo.
echo  Stopped. Start again: double-click start.bat
echo.
pause
