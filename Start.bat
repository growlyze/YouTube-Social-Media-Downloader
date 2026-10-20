@echo off
rem Double-click to start the Local Media Downloader in Docker and open it in the browser.
title Local Media Downloader
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker Desktop is not installed.
  echo Install it from https://www.docker.com/products/docker-desktop/ and run Start.bat again.
  pause
  exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
  echo Starting Docker Desktop...
  if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
  for /l %%i in (1,1,60) do (
    timeout /t 3 /nobreak >nul
    docker info >nul 2>nul && goto docker_ready
  )
  echo Docker Desktop did not start. Open it manually, wait until it says "Engine running", then run Start.bat again.
  pause
  exit /b 1
)
:docker_ready

if not exist ".env" copy ".env.example" ".env" >nul
if not exist "downloads" mkdir "downloads"

set "PORT=5000"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do if /i "%%a"=="YTDL_PORT" if not "%%b"=="" set "PORT=%%b"

echo Building and starting the app (the first start takes a few minutes)...
docker compose up -d --build
if errorlevel 1 (
  echo Could not start the app. See the messages above.
  pause
  exit /b 1
)

echo Waiting for the app to be ready...
for /l %%i in (1,1,40) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:%PORT%/ | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul && goto app_ready
  timeout /t 2 /nobreak >nul
)
echo The app did not answer yet. Run "docker compose logs" in this folder to see why.
pause
exit /b 1

:app_ready
start "" "http://127.0.0.1:%PORT%/"
echo The app is running at http://127.0.0.1:%PORT%/  (double-click Stop.bat to stop it)
timeout /t 5 >nul
