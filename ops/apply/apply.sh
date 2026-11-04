#!/bin/sh
# One apply container runs a deploy: build (unless SKIP_BUILD), then compose up.
# A healthcheck the app's compose file defines is the gate for `up --wait`; without
# one the deploy ends once the container runs. The env is the only input. Every
# step prints `==` so `podman logs deploy-<name>` reads as the full story.
set -eu

: "${COMPOSE_PROJECT_NAME:?}" "${COMPOSE_FILE:?}"

dc() {
  docker-compose "$@"
}

# The repo compose has to put service app on network yard. A missing env file or
# data mount is a warning. Yard's own files (YARD_FILES=1) meet the contract.
check() {
  [ -n "${YARD_FILES:-}" ] && return 0
  oldifs=$IFS
  IFS=:
  for file in $COMPOSE_FILE; do
    [ -f "$file" ] || continue
    grep -q '\${APP_ENV}' "$file" || echo "warning: compose: no service attaches env_file \${APP_ENV}; the project env is not passed"
    grep -q '\${DATA_DIR}' "$file" || echo "warning: compose: no service mounts \${DATA_DIR}; the project has no /data"
  done
  IFS=$oldifs
  cfg=$(dc config) || exit 1
  printf '%s\n' "$cfg" | awk '
    /^  app:/ { inapp = 1; next }
    inapp && /^  [^ ]/ { inapp = 0 }
    inapp && /^    networks:/ { innet = 1; next }
    inapp && innet && /^    [^ ]/ { innet = 0 }
    inapp && innet && (($1 == "-" && $2 == "yard") || $1 == "yard:") { found = 1 }
    END { exit found ? 0 : 1 }
  ' || {
    echo "compose: service app is not on network yard; the dial is ${COMPOSE_PROJECT_NAME}-app-1:8000" >&2
    exit 1
  }
}

build() {
  echo "== build $IMAGE"
  if [ -n "${YARD_FILES:-}" ]; then
    # Yard's build files: one image from CHECKOUT; podman-remote builds it
    # in ~2s where dc build needs ~18s on the same socket.
    # Docker format keeps HEALTHCHECK; podman-remote defaults to OCI, which drops it.
    # An absolute DOCKERFILE is the one Yard wrote outside the checkout.
    df="${DOCKERFILE:-Containerfile}"
    case "$df" in
      /*) ;;
      *) df="$CHECKOUT/$df" ;;
    esac
    podman-remote build --format docker --pull=missing -t "$IMAGE" -f "$df" "$CHECKOUT" || exit 1
  else
    dc build || exit 1
  fi
  echo "built $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}

revert() {
  if [ -n "${PREVIOUS_COMPOSE_FILE:-}" ]; then
    echo "== revert $PREVIOUS_COMPOSE_FILE"
    if COMPOSE_FILE="$PREVIOUS_COMPOSE_FILE" dc up -d --no-build; then
      echo "== reverted $PREVIOUS_COMPOSE_FILE"
      exit 3
    fi
    echo "== revert failed"
    exit 1
  fi
  if [ -n "${PREVIOUS_IMAGE:-}" ]; then
    echo "== revert $PREVIOUS_IMAGE"
    if IMAGE="$PREVIOUS_IMAGE" dc up -d --no-build; then
      echo "== reverted $PREVIOUS_IMAGE"
      exit 3
    fi
    echo "== revert failed"
    exit 1
  fi
  exit 1
}

up() {
  if [ "${SKIP_BUILD:-}" = "1" ]; then
    echo "== reuse $IMAGE"
  else
    build
  fi
  echo "== start $IMAGE"
  dc up -d --wait --wait-timeout 120 --remove-orphans --no-build || {
    echo "== unhealthy: up failed"
    revert
  }
  echo "== started $IMAGE"
}

case "${1:-}" in
  up)
    check
    up
    ;;
  purge) dc down -v ;;
  *)
    echo "usage: apply {up|purge}" >&2
    exit 2
    ;;
esac
