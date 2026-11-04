@echo off
chcp 65001 >nul
echo ========================================================
echo   OmniDub Studio - Xem Real-time Log tat ca cac Services
echo   (Nhan phim Ctrl + C de thoat man hinh log)
echo ========================================================
wsl -u root -d Ubuntu -e bash -c "cd /mnt/d/translate && docker compose logs -f --tail 100"
pause
