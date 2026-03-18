#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/etc/default/imagingedge-liveview"

CAMERA_IP="${CAMERA_IP:-192.168.122.1}"
CAMERA_PORT="${CAMERA_PORT:-10000}"
WIFI_INTERFACE="${WIFI_INTERFACE:-auto}"
WIFI_PASSWORD="${WIFI_PASSWORD:-}"
LISTEN_ADDRESS="${LISTEN_ADDRESS:-0.0.0.0}"
LISTEN_PORT="${LISTEN_PORT:-8765}"
STILLS_INTERVAL_MS="${STILLS_INTERVAL_MS:-500}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

args=(
  --address "${CAMERA_IP}"
  --camera-port "${CAMERA_PORT}"
  --wifi-interface "${WIFI_INTERFACE}"
  --listen "${LISTEN_ADDRESS}"
  --port "${LISTEN_PORT}"
  --stills-interval-ms "${STILLS_INTERVAL_MS}"
)

if [[ -n "${WIFI_PASSWORD}" ]]; then
  args+=(--wifi-password "${WIFI_PASSWORD}")
fi

exec "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/liveview_webui.py" "${args[@]}"
