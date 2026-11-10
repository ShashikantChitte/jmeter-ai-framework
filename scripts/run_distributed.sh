#!/usr/bin/env bash
# Controller entrypoint: discovers workers (headless-service DNS), waits for RMI, runs the test.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${REMOTE_HOSTS:-}" ]]; then
  : "${WORKERS_DNS:?set WORKERS_DNS (headless service) or REMOTE_HOSTS}"
  : "${EXPECTED_WORKERS:=1}"
  echo ">> Resolving workers via $WORKERS_DNS (expecting $EXPECTED_WORKERS)"
  for i in $(seq 1 60); do
    REMOTE_HOSTS=$(getent hosts "$WORKERS_DNS" | awk '{print $1}' | sort -u | paste -sd, - || true)
    COUNT=$(awk -F, '{print NF}' <<<"${REMOTE_HOSTS:-}")
    [[ -n "$REMOTE_HOSTS" && "$COUNT" -ge "$EXPECTED_WORKERS" ]] && break
    sleep 5
  done
fi
for ip in ${REMOTE_HOSTS//,/ }; do
  for i in $(seq 1 60); do (echo > "/dev/tcp/$ip/1099") 2>/dev/null && break || sleep 3; done
done
export REMOTE_HOSTS
exec "$ROOT/scripts/run_test.sh" "$@"
