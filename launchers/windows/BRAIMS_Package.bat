@echo off
rem BRAIMS Package: build the upload package in the outbox folder (refused while the check finds errors).
title BRAIMS Package
cd /d "%~dp0"
set DOCKER_CLI_HINTS=false
docker run --rm -v "%CD%:/data" ghcr.io/jonadabsilva/braims:1.0.0 package
echo.
pause
