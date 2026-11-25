#!/bin/bash
# BRAIMS Run: sort, name, convert, remove neck and face, check, report.
# Keep this file in your BRAIMS folder, next to the raw folder, and double-click it.
cd "$(dirname "$0")" || exit 1
export DOCKER_CLI_HINTS=false
docker run --rm --platform linux/amd64 -v "$PWD:/data" ghcr.io/jonadabsilva/braims:1.0.0 run
echo
read -r -p "Finished. Open the newest file in the reports folder. Press Enter to close. " _
