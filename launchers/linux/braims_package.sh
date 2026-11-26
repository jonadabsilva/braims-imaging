#!/bin/bash
# BRAIMS Package. Keep this file in your BRAIMS folder, next to the raw folder, and double-click it.
cd "$(dirname "$0")" || exit 1
export DOCKER_CLI_HINTS=false
case "$PWD" in *OneDrive*|*Dropbox*|*"Google Drive"*|*CloudDocs*|*iCloud*)
  echo "This BRAIMS folder is inside a cloud-synchronised folder: exams would be copied to the cloud."
  echo "Move the BRAIMS folder to a local disk and run again."; read -r -p "Press Enter to close. " _; exit 1;; esac
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop, wait until it says it is running, then double-click again."
  read -r -p "Press Enter to close. " _; exit 1
fi
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/data" ghcr.io/jonadabsilva/braims:1.1.0 package
echo
read -r -p "Finished. Press Enter to close. " _
