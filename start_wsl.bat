@echo off
chcp 65001 >nul
echo ========================================================
echo   OmniDub Studio - Khoi dong he thong tren WSL2
echo ========================================================
echo Dang khoi chay cac microservices bang WSL Docker...
wsl -u root -d Ubuntu -e bash -c "cd /mnt/d/translate && docker compose up -d"

if %ERRORLEVEL% NEQ 0 (
    echo [LOI] Khong the khoi dong du an qua WSL!
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   TAT CA CAC SERVICES DA KHOI DONG THANH CONG!
echo   Giao dien Web: http://localhost:7860
echo ========================================================
pause
