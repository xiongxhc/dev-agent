#!/usr/bin/env bash
# Build the M2 sandbox image (the product runtime that builds + verifies user apps).
#
#   ./build.sh            # local: native-arch image loaded into THIS machine's docker (dev)
#   ./build.sh multiarch  # deploy: linux/amd64 + linux/arm64, PUSHED to $REGISTRY
#
# Why two modes: this Mac is arm64, but most servers are amd64 — an arm64-only image won't
# run there. Multi-arch needs buildx's docker-container driver and (per a Docker limitation)
# can only output to a registry, so `multiarch` implies a push. The `local` build stays the
# fast path for iterating here.
set -euo pipefail

IMAGE="${DEVAGENT_M2_IMAGE:-devagent-sandbox:m2}"
DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-local}"

case "$MODE" in
  local)
    docker build -f "$DIR/Dockerfile.m2" -t "$IMAGE" "$DIR"
    ;;
  multiarch)
    : "${REGISTRY:?set REGISTRY=ghcr.io/<you>/devagent-sandbox  (multi-arch must push to a registry)}"
    # Idempotent: create the multi-arch builder once, reuse after.
    docker buildx inspect devagent-builder >/dev/null 2>&1 \
      || docker buildx create --name devagent-builder --driver docker-container >/dev/null
    docker buildx build --builder devagent-builder \
      --platform linux/amd64,linux/arm64 \
      -f "$DIR/Dockerfile.m2" -t "$REGISTRY:m2" --push "$DIR"
    ;;
  *)
    echo "usage: $0 [local|multiarch]" >&2
    exit 2
    ;;
esac
