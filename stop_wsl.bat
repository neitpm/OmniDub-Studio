@echo off
chcp 65001 >nul
echo Dang dung toan bo cac microservices trong WSL...
wsl -u root -d Ubuntu -e bash -c "cd /mnt/d/translate && docker compose down"
echo Da dung tat ca cac services!
pause
