#!/usr/bin/env bash
# Readiness check for a local v0.9.7 xiaozhi-server. It does not change
# macOS privacy settings, move files, or start as root.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "preflight: refusing to run as root. This script will not change privileges." >&2
  exit 77
fi

if [ -z "${XIAOZHI_SERVER_DIR:-}" ]; then
  echo "preflight: set XIAOZHI_SERVER_DIR to your xiaozhi-server directory." >&2
  exit 64
fi

XIAOZHI_PYTHON="${XIAOZHI_PYTHON:-python3}"
XIAOZHI_WS_PORT="${XIAOZHI_WS_PORT:-8000}"
XIAOZHI_HTTP_PORT="${XIAOZHI_HTTP_PORT:-8003}"
XIAOZHI_MEM0_HOST="${XIAOZHI_MEM0_HOST:-127.0.0.1}"
XIAOZHI_MEM0_PORT="${XIAOZHI_MEM0_PORT:-8888}"
XIAOZHI_MEM0_ATTEMPTS="${XIAOZHI_MEM0_ATTEMPTS:-5}"
XIAOZHI_MEM0_DELAY_SEC="${XIAOZHI_MEM0_DELAY_SEC:-1}"

if [ ! -r "${XIAOZHI_SERVER_DIR}/app.py" ]; then
  echo "preflight: cannot read ${XIAOZHI_SERVER_DIR}/app.py." >&2
  echo "preflight: if launchd was denied Desktop access, change that in System Settings yourself or run from a directory you can read. This script will not change privacy settings or move private data." >&2
  exit 73
fi

if [ ! -f "${XIAOZHI_SERVER_DIR}/data/.config.yaml" ]; then
  echo "preflight: data/.config.yaml is missing. Not creating or printing configuration." >&2
  exit 75
fi

if [ ! -r "${XIAOZHI_SERVER_DIR}/data/.config.yaml" ]; then
  echo "preflight: data/.config.yaml is not readable. Not changing permissions." >&2
  exit 73
fi

if ! "${XIAOZHI_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 10) else 1)'; then
  echo "preflight: Python 3.10 is required (the live host uses 3.10.18). Not installing another interpreter." >&2
  exit 74
fi

if [ -n "${XIAOZHI_MEM0_START_CMD:-}" ]; then
  case "${XIAOZHI_MEM0_START_CMD}" in
    *sudo*|*launchctl*)
      echo "preflight: refusing a Mem0 start command that uses sudo or launchctl." >&2
      exit 76
      ;;
  esac
  echo "preflight: running the user-supplied Mem0 start command once." >&2
  bash -c "${XIAOZHI_MEM0_START_CMD}"
fi

if [ -n "${XIAOZHI_MEM0_CONTAINER:-}" ] && command -v docker >/dev/null 2>&1; then
  docker inspect -f '{{.State.Status}}' "${XIAOZHI_MEM0_CONTAINER}" >&2 || echo "preflight: docker inspect failed; continuing with the port check." >&2
fi

export XIAOZHI_PYTHON XIAOZHI_WS_PORT XIAOZHI_HTTP_PORT
export XIAOZHI_MEM0_HOST XIAOZHI_MEM0_PORT XIAOZHI_MEM0_ATTEMPTS XIAOZHI_MEM0_DELAY_SEC
export XIAOZHI_MEM0_HEALTH_URL="${XIAOZHI_MEM0_HEALTH_URL:-}"

"${XIAOZHI_PYTHON}" - <<'PY'
import os
import socket
import time
import urllib.error
import urllib.request

def busy(port):
    probe = socket.socket()
    probe.settimeout(0.2)
    try:
        return probe.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        probe.close()

for name in ("XIAOZHI_WS_PORT", "XIAOZHI_HTTP_PORT"):
    port = int(os.environ[name])
    if busy(port):
        print(f"preflight: {name}={port} is already in use. Not stopping that process.")
        raise SystemExit(76)

host = os.environ["XIAOZHI_MEM0_HOST"]
port = int(os.environ["XIAOZHI_MEM0_PORT"])
attempts = max(1, min(int(os.environ["XIAOZHI_MEM0_ATTEMPTS"]), 10))
delay = max(0.0, float(os.environ["XIAOZHI_MEM0_DELAY_SEC"]))
health = os.environ.get("XIAOZHI_MEM0_HEALTH_URL", "").strip()
ready = False
for _ in range(attempts):
    tcp_ok = False
    try:
        with socket.create_connection((host, port), 1):
            tcp_ok = True
    except OSError:
        tcp_ok = False
    if tcp_ok and not health:
        ready = True
        break
    if tcp_ok and health:
        code = 0
        try:
            with urllib.request.urlopen(health, timeout=2) as response:
                code = int(response.status)
                response.read(64)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
        except Exception:
            code = 0
        if code and code not in (502, 503):
            ready = True
            break
    if delay:
        time.sleep(delay)

if not ready:
    print(f"preflight: Mem0 {host}:{port} was not ready after {attempts} attempts.")
    raise SystemExit(78)
print(f"preflight: mem0_tcp_ready host={host} port={port}")
PY

if [ "${XIAOZHI_HOSTNAME_CHECK:-0}" = "1" ]; then
  if [ -z "${XIAOZHI_HOSTNAME:-}" ]; then
    echo "preflight: XIAOZHI_HOSTNAME_CHECK=1 requires XIAOZHI_HOSTNAME." >&2
    exit 64
  fi
  if "${XIAOZHI_PYTHON}" -c 'import socket,sys; socket.getaddrinfo(sys.argv[1], None)' "${XIAOZHI_HOSTNAME}"; then
    echo "preflight: hostname_resolved=1 name=${XIAOZHI_HOSTNAME}"
  else
    echo "preflight: hostname_resolved=0 name=${XIAOZHI_HOSTNAME}" >&2
    exit 79
  fi
  echo "preflight: hostname check does not rewrite OTA, websocket, or firmware settings."
fi

echo "preflight: ok ws_port=${XIAOZHI_WS_PORT} http_port=${XIAOZHI_HTTP_PORT}"
