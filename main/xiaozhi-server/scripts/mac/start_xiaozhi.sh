#!/usr/bin/env bash
# Foreground start with a short retry if the process exits immediately.
# This does not install launchd and does not claim login or boot reliability.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${here}/preflight_xiaozhi.sh"

cd "${XIAOZHI_SERVER_DIR}"
attempts="${XIAOZHI_START_RETRIES:-3}"
delay="${XIAOZHI_RETRY_DELAY_SEC:-2}"
grace="${XIAOZHI_START_GRACE_SEC:-3}"
if [ "${attempts}" -gt 5 ]; then
  attempts=5
fi
if [ "${attempts}" -lt 1 ]; then
  attempts=1
fi

n=1
while [ "${n}" -le "${attempts}" ]; do
  "${XIAOZHI_PYTHON}" app.py &
  pid=$!
  sleep "${grace}"
  if kill -0 "${pid}" 2>/dev/null; then
    wait "${pid}"
    exit $?
  fi
  wait "${pid}" || true
  echo "start: server exited before ${grace}s attempt=${n}. Not a boot-reliability result." >&2
  n=$((n + 1))
  if [ "${n}" -le "${attempts}" ]; then
    sleep "${delay}"
  fi
done

echo "start: stopped after ${attempts} attempts." >&2
exit 1
