@echo off
title ChatFlow - Stop
chcp 65001 > nul

echo ============================================
echo   ChatFlow Docker Compose Stop
echo ============================================
echo.

set "COMPOSE_DIR=%~dp0llm-chat"

REM -- Stop cloudflared tunnel --
echo [1/3] Stopping Cloudflare Tunnel...
taskkill /f /im cloudflared.exe > nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Cloudflare Tunnel stopped.
) else (
    echo [INFO] Cloudflare Tunnel was not running.
)

REM -- Check Docker is running --
docker info > nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker is not running.
    pause
    exit /b 1
)

cd /d "%COMPOSE_DIR%"

echo.
echo [2/3] Stopping all Docker services...
docker compose down
if %errorlevel% neq 0 (
    echo [WARN] docker compose down reported an error.
)

echo.
echo [3/3] Verifying all containers are stopped...
docker compose ps
echo.

echo ============================================
echo   All services stopped.
echo ============================================
pause