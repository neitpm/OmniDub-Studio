@echo off
title Start AI Dubbing Microservices
echo ===================================================
echo     DANG KHOI DONG HE THONG AI DUBBING & SUBTITLING
echo ===================================================
cd /d D:\translate

echo [1/3] Kiem tra Docker Desktop...
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Docker Desktop chua bat! Dang khoi dong Docker Desktop...
    start "" "C:\Users\MSI\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe"
    echo [i] Vui long doi Docker Desktop khoi dong trong giay lat...
    :wait_docker
    timeout /t 5 /nobreak >nul
    docker info >nul 2>&1
    if %errorlevel% neq 0 goto wait_docker
    echo [OK] Docker Desktop da san sang!
)

echo [2/3] Khoi dong tat ca 5 Microservices...
docker compose up -d

echo [3/3] Kiem tra trang thai...
docker ps --filter "name=translate" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo.
echo ===================================================
echo   TAT CA SERVICES DA HOAT DONG!
echo   Mo trinh duyet tai: http://localhost:7860
echo ===================================================
echo.
start http://localhost:7860
pause
