@echo off
rem BRAIMS Run. Keep this file in your BRAIMS folder, next to the raw folder, and double-click it.
title BRAIMS Run
cd /d "%~dp0"
set DOCKER_CLI_HINTS=false
echo %CD% | findstr /i "OneDrive Dropbox Google iCloud" >nul && (
  echo This BRAIMS folder is inside a cloud-synchronised folder: exams would be copied to the cloud.
  echo Move the BRAIMS folder to a local disk, for example C:\BRAIMS, and run again.
  pause & exit /b 1 )
docker info >nul 2>&1 || (
  echo Docker is not running. Start Docker Desktop, wait until it says it is running, then double-click again.
  pause & exit /b 1 )
docker run --rm -v "%CD%:/data" ghcr.io/jonadabsilva/braims:1.1.0 run
echo.
pause
