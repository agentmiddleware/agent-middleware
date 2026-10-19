#!/usr/bin/env bash
# Build both gateway images and import the modules needed at app startup.
set -euo pipefail

if [[ -e .build_commit_sha ]]; then
  echo '.build_commit_sha already exists; refusing to replace it' >&2
  exit 1
fi
if [[ -e gateway/.build_commit_sha ]]; then
  echo 'gateway/.build_commit_sha already exists; refusing to replace it' >&2
  exit 1
fi

stamp="$(git rev-parse HEAD)"
other=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
printf '%s\n' "$stamp" > .build_commit_sha
printf '%s\n' "$other" > gateway/.build_commit_sha
trap 'rm -f .build_commit_sha gateway/.build_commit_sha' EXIT

docker build -f gateway/Dockerfile -t amw-gateway:ci-release .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-release -c '
import sys
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert Path("/app/.build_commit_sha").read_bytes() == (sys.argv[1] + "\n").encode()
' "$stamp"

rm .build_commit_sha
docker build -f gateway/Dockerfile.dev -t amw-gateway:ci-dev .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-dev -c '
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert not Path("/app/.build_commit_sha").exists()
'

# Negative cases build only the small metadata stage. The nested stamp remains
# on disk but is excluded from the root Docker context by .dockerignore.
expect_metadata_failure() {
  if docker build --target build-metadata -f gateway/Dockerfile "$@" .; then
    echo "metadata stage accepted invalid provenance: $*" >&2
    exit 1
  fi
}

check_metadata_stamp() {
  local image="$1"
  docker run --rm --network none --read-only --user 65534:65534 \
    --entrypoint python "$image" -c '
import sys
from pathlib import Path
assert Path("/stamp/.build_commit_sha").read_bytes() == (sys.argv[1] + "\n").encode()
' "$stamp"
}

# A gateway-local stamp cannot supply provenance, even when valid.
expect_metadata_failure

# Argument-only provenance is accepted and written in canonical lowercase.
docker build --target build-metadata -f gateway/Dockerfile \
  --build-arg "RELEASE_COMMIT_SHA=${stamp^^}" -t amw-gateway:ci-metadata .
check_metadata_stamp amw-gateway:ci-metadata

rm gateway/.build_commit_sha
expect_metadata_failure  # neither root stamp nor argument exists
COMMIT_SHA="$stamp" BUILD_COMMIT_SHA="$stamp" expect_metadata_failure

printf '%s\n' "${stamp^^}" > .build_commit_sha
docker build --target build-metadata -f gateway/Dockerfile \
  --build-arg "RELEASE_COMMIT_SHA=$stamp" -t amw-gateway:ci-metadata .
check_metadata_stamp amw-gateway:ci-metadata

expect_metadata_failure --build-arg "RELEASE_COMMIT_SHA=$other"
expect_metadata_failure --build-arg RELEASE_COMMIT_SHA=not-a-sha

printf 'not-a-sha\n' > .build_commit_sha
expect_metadata_failure --build-arg "RELEASE_COMMIT_SHA=$stamp"
