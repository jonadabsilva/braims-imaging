@echo off
rem BRAIMS Run: sort, name, convert, remove neck and face, check, report.
rem Keep this file in your BRAIMS folder, next to the raw folder, and double-click it.
title BRAIMS Run
cd /d "%~dp0"
set DOCKER_CLI_HINTS=false
docker run --rm -v "%CD%:/data" ghcr.io/jonadabsilva/braims:1.0.0 run
echo.
echo Finished. Open the newest file in the reports folder.
pause
