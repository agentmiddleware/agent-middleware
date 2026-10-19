#!/usr/bin/env bash
# Build both gateway images and import the modules needed at app startup.
set -euo pipefail

if [[ -e .build_commit_sha ]]; then
  echo '.build_commit_sha already exists; refusing to replace it' >&2
  exit 1
fi

stamp="$(git rev-parse HEAD)"
printf '%s\n' "$stamp" > .build_commit_sha
trap 'rm -f .build_commit_sha' EXIT

docker build -f gateway/Dockerfile -t amw-gateway:ci-release .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-release -c '
import sys
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert Path("/app/.build_commit_sha").read_text().strip() == sys.argv[1]
' "$stamp"

rm .build_commit_sha
docker build -f gateway/Dockerfile.dev -t amw-gateway:ci-dev .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-dev -c '
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert not Path("/app/.build_commit_sha").exists()
'
