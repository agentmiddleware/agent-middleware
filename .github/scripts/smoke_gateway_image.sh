#!/usr/bin/env bash
# Build both gateway images and import the modules needed at app startup.
set -euo pipefail

if [[ -e .build_commit_sha || -L .build_commit_sha ]]; then
  echo '.build_commit_sha already exists; refusing to replace it' >&2
  exit 1
fi
if [[ -e gateway/.build_commit_sha || -L gateway/.build_commit_sha ]]; then
  echo 'gateway/.build_commit_sha already exists; refusing to replace it' >&2
  exit 1
fi

canary_paths=(
  gateway/data/quickstart/signing-seed.b64
  .amw-image-smoke.db-wal
  gateway/.amw-image-smoke.db-shm
  gateway/.amw-image-smoke.log
  gateway/.amw-image-smoke.pem
  gateway/.agents/amw-image-smoke.txt
  gateway/.claude/amw-image-smoke.txt
  gateway/.envrc
  sdk/python/.amw-image-smoke.key
)
for path in "${canary_paths[@]}"; do
  if [[ -e "$path" || -L "$path" ]]; then
    echo "Canary path already exists: $path" >&2
    exit 1
  fi
done

created_dirs=()
created_files=()
stamp_created=false
cleanup() {
  if [[ "$stamp_created" == true ]]; then rm -f .build_commit_sha; fi
  for path in "${created_files[@]}"; do rm -f "$path"; done
  for ((i=${#created_dirs[@]}-1; i>=0; i--)); do
    rmdir "${created_dirs[i]}" 2>/dev/null || true
  done
}
trap cleanup EXIT

for dir in gateway/data gateway/data/quickstart gateway/.agents gateway/.claude; do
  if [[ -L "$dir" ]]; then
    echo "Canary parent is a symlink: $dir" >&2
    exit 1
  fi
  if [[ ! -d "$dir" ]]; then
    mkdir "$dir"
    created_dirs+=("$dir")
  fi
done
for path in "${canary_paths[@]}"; do
  printf 'synthetic image context canary\n' > "$path"
  created_files+=("$path")
done

# A named COPY of an ignored canary must fail before creating any layer.
for path in "${canary_paths[@]}"; do
  if printf 'FROM scratch\nCOPY %s /probe/\n' "$path" | docker build -f - . >/dev/null 2>&1; then
    echo "Docker context unexpectedly includes $path" >&2
    exit 1
  fi
done

image_canaries=(
  /app/data/quickstart/signing-seed.b64
  /app/.amw-image-smoke.db-shm
  /app/.amw-image-smoke.log
  /app/.amw-image-smoke.pem
  /app/.agents/amw-image-smoke.txt
  /app/.claude/amw-image-smoke.txt
  /app/.envrc
  /sdk/python/.amw-image-smoke.key
)
stamp="$(git rev-parse HEAD)"
other=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
printf '%s\n' "$stamp" > .build_commit_sha
printf '%s\n' "$other" > gateway/.build_commit_sha
stamp_created=true
created_files+=(gateway/.build_commit_sha)

docker build -f gateway/Dockerfile -t amw-gateway:ci-release .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-release -c '
import sys
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert Path("/app/.build_commit_sha").read_bytes() == (sys.argv[1] + "\n").encode()
assert all(not Path(path).exists() for path in sys.argv[2:])
' "$stamp" "${image_canaries[@]}"

docker build --target build-metadata -f gateway/Dockerfile -t amw-gateway:ci-metadata .
docker run --rm --network none --read-only --user 65534:65534 \
  --entrypoint python amw-gateway:ci-metadata -c '
import sys
from pathlib import Path
root = Path("/context")
expected = (sys.argv[1] + "\n").encode()
assert (root / ".build_commit_sha").read_bytes() == expected
assert Path("/stamp/.build_commit_sha").read_bytes() == expected
assert (root / "sdk/python/src/b2a_sdk/receipt_verifier.py").is_file()
assert (root / "gateway/scripts/write_build_commit_sha.py").is_file()
assert not (root / "gateway/data").exists()
assert not (root / "gateway/.build_commit_sha").exists()
assert all(not (root / path).exists() for path in sys.argv[2:])
' "$stamp" "${canary_paths[@]}"

rm .build_commit_sha
docker build -f gateway/Dockerfile.dev -t amw-gateway:ci-dev .
docker run --rm --network none --read-only --entrypoint python amw-gateway:ci-dev -c '
import sys
from pathlib import Path
import app.main
import b2a_sdk.receipt_verifier
assert not Path("/app/.build_commit_sha").exists()
assert all(not Path(path).exists() for path in sys.argv[1:])
' "${image_canaries[@]}"

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

# CI and Railway build args supply the commit without a stamp file.
docker build --target build-metadata -f gateway/Dockerfile \
  --build-arg "GIT_COMMIT_SHA=$stamp" -t amw-gateway:ci-metadata .
check_metadata_stamp amw-gateway:ci-metadata
docker build --target build-metadata -f gateway/Dockerfile \
  --build-arg "RAILWAY_GIT_COMMIT_SHA=$stamp" -t amw-gateway:ci-metadata .
check_metadata_stamp amw-gateway:ci-metadata
# A stale pinned override fails instead of stamping the wrong commit.
expect_metadata_failure --build-arg "RELEASE_COMMIT_SHA=$other" \
  --build-arg "RAILWAY_GIT_COMMIT_SHA=$stamp"

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
