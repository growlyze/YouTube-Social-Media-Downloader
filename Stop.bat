@echo off
rem Double-click to stop the Local Media Downloader. Downloads and settings are kept.
title Local Media Downloader
cd /d "%~dp0"
docker compose down
echo The app has stopped. Your videos, settings and Google sign-in are kept.
timeout /t 5 >nul
