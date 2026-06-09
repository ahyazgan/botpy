@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title Kripto Haber Trade - Radar
echo ================================================
echo  Kripto Haber Trade - Radar baslatiliyor
echo ================================================
echo.
echo  1) Haber motoru (API)      : http://127.0.0.1:8000
echo  2) Panel (tarayicida acin) : http://localhost:5173
echo.
echo  Guclu haber cikinca masaustu bildirimi gelecek.
echo  Durdurmak icin bu pencereleri kapatin.
echo.

REM Haber motorunu ayri pencerede baslat
start "Haber Motoru" cmd /k "chcp 65001 >nul && set PYTHONUTF8=1 && cd /d %~dp0 && python news_bot.py"

REM Paneli ayri pencerede baslat
start "Panel" cmd /k "cd /d %~dp0dashboard && npm run dev"

echo Iki pencere acildi. Panel hazir olunca tarayicida http://localhost:5173 acin.
pause
