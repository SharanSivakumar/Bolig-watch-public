#!/usr/bin/env bash
# Convenience wrapper for cron or systemd.
#
#   ./run.sh                 # one scan
#   ./run.sh --dry-run       # one scan, no alerts
#   ./run.sh --refresh-local # re-score stored listings, no portal request
#
# It uses .venv when present, waits briefly for DNS after the machine wakes, and
# forwards every argument to run.py.
set -euo pipefail

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# A timer that fires while the machine is still waking can run before Wi-Fi is
# back. Wait up to five minutes for the first portal to resolve, then run anyway
# so a real failure is recorded instead of silently skipped.
if [ "${1:-}" != "--refresh-local" ] && [ "${1:-}" != "--rescore" ]; then
  for _ in $(seq 1 60); do
    getent hosts www.boligportal.dk >/dev/null 2>&1 && break
    sleep 5
  done
fi

exec python run.py "$@"
