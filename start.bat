@echo off
rem ============================================================
rem  WebPilot one-click start (Windows: double-click this file)
rem  Steps: check .env -> make sure Docker runs -> compose up -> wait until
rem  http://localhost:8080 answers, then open browser.
rem  Stop: stop.bat (data volumes are kept).
rem  NOTE: keep this file pure ASCII. Chinese text in a UTF-8 .bat gets
rem        mangled by cmd on non-65001 code pages (tested, it breaks parsing).
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
title WebPilot starter
cd /d "%~dp0"

echo.
echo  ============================================
echo   WebPilot starting...
echo  ============================================
echo.

rem ---- step 1: .env (first run after clone: create from example and open editor) ----
if not exist ".env" (
    echo [1/4] No .env found. Created from .env.example, opening Notepad...
    echo       Only ONE thing to do: paste your LLM_API_KEY (free tier is fine,
    echo       get one at https://open.bigmodel.cn), then CLOSE Notepad to continue.
    copy /y ".env.example" ".env" >nul
    notepad ".env"
) else (
    echo [1/4] .env found, skip.
)
echo.

rem ---- warn if LLM_API_KEY is still empty (service starts, but tasks will fail) ----
set "LLM_KEY="
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="LLM_API_KEY" set "LLM_KEY=%%b"
)
if "%LLM_KEY%"=="" (
    echo       [WARN] LLM_API_KEY is EMPTY. Stack will start, but tasks will fail.
    echo              Fix later: edit .env, then run stop.bat and start.bat again.
    echo.
) else (
    echo       LLM_API_KEY is set.
)

rem ---- step 2: make sure the Docker engine is up (start Docker Desktop if needed) ----
echo [2/4] Checking Docker...
docker info >nul 2>&1
if not errorlevel 1 goto docker_ok

set "DD=C:\Program Files\Docker\Docker\Docker Desktop.exe"
if not exist "%DD%" (
    echo       [ERROR] Docker is not running and Docker Desktop was not found.
    echo               Install / start Docker Desktop, then run this script again.
    pause
    exit /b 1
)
echo       Docker is not running. Starting Docker Desktop, please wait...
start "" "%DD%"
set /a TRIES=0

:wait_docker
timeout /t 3 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto docker_ok
set /a TRIES+=1
if !TRIES! geq 40 (
    echo       [ERROR] Docker engine not ready after 120s. Start Docker Desktop
    echo               manually, wait for it to turn green, then run this again.
    pause
    exit /b 1
)
goto wait_docker

:docker_ok
echo       Docker is ready.
echo.

rem ---- step 3: compose up (first run builds images, ~20 min; then cached, seconds) ----
set "FIRST_RUN=0"
docker image inspect webpilot-backend:local >nul 2>&1
if errorlevel 1 set "FIRST_RUN=1"
docker image inspect webpilot-web:local >nul 2>&1
if errorlevel 1 set "FIRST_RUN=1"

if "%FIRST_RUN%"=="1" (
    echo [3/4] First run: building images, takes about 20 MINUTES (Chromium deps).
    echo       Go grab a coffee - next launches will be fast \(cached layers\).
) else (
    echo [3/4] Starting 5 containers: pg / redis / api / worker / web
)
docker compose up -d
if errorlevel 1 (
    echo.
    echo       [ERROR] docker compose failed. Read the error above.
    pause
    exit /b 1
)
echo.

rem ---- step 4: wait until http://localhost:8080 answers (web up = api/pg/redis healthy) ----
echo [4/4] Waiting for http://localhost:8080 (polling every 5s, first run may take 30 min)...
set /a SECS=0

:wait_http
timeout /t 5 /nobreak >nul
set "CODE="
for /f %%i in ('curl -s -o nul -w "%%{http_code}" http://localhost:8080 2^>nul') do set "CODE=%%i"
if "%CODE%"=="200" goto ready
if "%CODE%"=="302" goto ready
set /a SECS+=5
rem progress line every 30s so it does not look frozen
set /a TICK=SECS %% 30
if "%TICK%"=="0" echo       waited %SECS%s ...
if %SECS% geq 1800 (
    echo.
    echo       [ERROR] Not ready after 30 min. Build or startup probably failed.
    echo       Debug: docker compose ps   /   docker compose logs --tail=50 api
    pause
    exit /b 1
)
goto wait_http

:ready
echo       Ready!
echo.
start "" "http://localhost:8080"
echo  ============================================
echo   Done. Browser opened at http://localhost:8080
echo   First time? Register an account, then submit a task.
echo   Stop: double-click stop.bat (data is kept)
echo  ============================================
echo.
pause
