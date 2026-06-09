@echo off
chcp 65001 >nul
title Polymarket Arbitraj - Bildirim Modu
echo Arbitraj botu baslatiliyor (sadece bildirim modu)...
echo Firsat cikinca masaustu bildirimi gelecek. Durdurmak icin: Ctrl+C
echo.
python "%~dp0arb_bot.py"
pause
