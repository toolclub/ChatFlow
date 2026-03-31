@echo off
title ChatFlow - Start
chcp 65001 > nul

echo ============================================
echo   ChatFlow Docker Compose Start
echo ============================================
echo.

set "ROOT_DIR=%~dp0"
set "COMPOSE_DIR=%~dp0llm-chat"
set "CLOUDFLARED=%~dp0cloudflared.exe"
set "CLOUDFLARED_CONFIG=%~dp0cloudflared-config.yml"

REM -- Check Docker is running --
docker info > nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker is not running. Please start Docker Desktop first.
    pause
    exit /b 1
)

REM -- Create logs directory (mounted into container) --
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo [1/3] Building and starting all services...
cd /d "%COMPOSE_DIR%"
docker compose up -d --build
if %errorlevel% neq 0 (
    echo [ERROR] docker compose up failed.
    pause
    exit /b 1
)

echo.
echo [2/3] Waiting for services to become healthy...
timeout /t 5 /nobreak > nul

REM -- Show container status --
docker compose ps
echo.

REM -- Start cloudflared tunnel in background --
echo [3/3] Starting Cloudflare Tunnel...
if not exist "%CLOUDFLARED%" (
    echo [WARN] cloudflared.exe not found, skipping tunnel.
) else if not exist "%CLOUDFLARED_CONFIG%" (
    echo [WARN] cloudflared-config.yml not found, skipping tunnel.
) else (
    REM Kill any existing cloudflared process first
    taskkill /f /im cloudflared.exe > nul 2>&1
    start /b "" "%CLOUDFLARED%" tunnel --config "%CLOUDFLARED_CONFIG%" run > "%~dp0logs\cloudflared.log" 2>&1
    timeout /t 2 /nobreak > nul
    echo [OK] Cloudflare Tunnel started (log: logs\cloudflared.log)
)

echo.
echo ============================================
echo   All services started!
echo ============================================
echo.
echo   Frontend:  http://localhost
echo   API docs:  http://localhost:8000/docs
echo   Qdrant:    http://localhost:6333/dashboard
echo   Tunnel:    https://chatflow-live.com
echo.
echo   Logs:
echo     App logs:   %~dp0logs\
echo     Tunnel log: %~dp0logs\cloudflared.log
echo     Container:  docker compose logs -f backend
echo.
echo   Run stop.bat to shut down all services.
echo ============================================
pause
