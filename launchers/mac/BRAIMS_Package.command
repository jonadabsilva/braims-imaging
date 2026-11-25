#!/bin/bash
# BRAIMS Package: build the upload package in the outbox folder (refused while the check finds errors).
cd "$(dirname "$0")" || exit 1
export DOCKER_CLI_HINTS=false
docker run --rm --platform linux/amd64 -v "$PWD:/data" ghcr.io/jonadabsilva/braims:1.0.0 package
echo
read -r -p "Press Enter to close. " _
