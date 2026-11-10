#!/usr/bin/env bash
# Run the framework in NON-GUI mode (local, Docker, CI or as controller of distributed run).
#   ENV=qa PROFILE=load ./scripts/run_test.sh [-Jany=override ...]
# Optional env vars: USERS RAMPUP DURATION TESTRUN REMOTE_HOSTS RUN_GATE BASELINE PLAN
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${ENV:=local}" "${PROFILE:=smoke}" "${PLAN:=tests/main/omp_main.jmx}"
: "${RESULTS_DIR:=$ROOT/results}" "${TESTRUN:=local-$(date +%Y%m%d-%H%M%S)}"
: "${JMETER_BIN:=jmeter}" "${RUN_GATE:=true}" "${REMOTE_HOSTS:=}" "${SHUTDOWN_REMOTE:=false}"

ENV_FILE="config/env/${ENV}.properties"
PROFILE_FILE="config/load_profiles/${PROFILE}.properties"
TUNING="config/jmeter/tuning.properties"
for f in "$ENV_FILE" "$PROFILE_FILE" "$PLAN"; do
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 64; }
done

mkdir -p "$RESULTS_DIR"
find "$RESULTS_DIR" -mindepth 1 -delete

ARGS=(-n -t "$PLAN" -q "$TUNING" -q "$ENV_FILE" -q "$PROFILE_FILE"
      -l "$RESULTS_DIR/results.jtl" -j "$RESULTS_DIR/jmeter.log"
      -e -o "$RESULTS_DIR/report"
      -Jbase_dir="$ROOT" -Jresults_dir="$RESULTS_DIR" -Jtestrun="$TESTRUN" -Jenv="$ENV")
[[ -n "${USERS:-}"    ]] && ARGS+=(-Jusers="$USERS")
[[ -n "${RAMPUP:-}"   ]] && ARGS+=(-Jrampup="$RAMPUP")
[[ -n "${DURATION:-}" ]] && ARGS+=(-Jduration="$DURATION")

if [[ -n "$REMOTE_HOSTS" ]]; then
  N=$(awk -F, '{print NF}' <<<"$REMOTE_HOSTS")
  # -G sends properties to every worker. Each worker runs the FULL plan, the plan divides users by 'workers'.
  ARGS+=(-R "$REMOTE_HOSTS" -G "$ENV_FILE" -G "$PROFILE_FILE" -Gworkers="$N" -Gbase_dir="$ROOT"
         -Gresults_dir="$RESULTS_DIR" -Gtestrun="$TESTRUN" -Genv="$ENV")
  [[ -n "${USERS:-}"    ]] && ARGS+=(-Gusers="$USERS")
  [[ -n "${RAMPUP:-}"   ]] && ARGS+=(-Grampup="$RAMPUP")
  [[ -n "${DURATION:-}" ]] && ARGS+=(-Gduration="$DURATION")
  [[ "$SHUTDOWN_REMOTE" == "true" ]] && ARGS+=(-X)
  echo ">> Distributed run on $N workers: $REMOTE_HOSTS"
fi
ARGS+=("$@")

echo ">> ENV=$ENV PROFILE=$PROFILE TESTRUN=$TESTRUN"
set +e
"$JMETER_BIN" "${ARGS[@]}"
RC=$?
set -e
[[ $RC -ne 0 ]] && { echo "JMeter exited with $RC" >&2; exit $RC; }

if [[ "$RUN_GATE" == "true" ]]; then
  RAMP=$(grep -hE '^rampup=' "$PROFILE_FILE" | tail -1 | cut -d= -f2)
  [[ -n "${RAMPUP:-}" ]] && RAMP="$RAMPUP"
  set +e
  python3 scripts/check_thresholds.py "$RESULTS_DIR/results.jtl" \
      --thresholds thresholds/thresholds.json --skip-sec "${RAMP:-0}" \
      --junit "$RESULTS_DIR/junit.xml" --summary "$RESULTS_DIR/summary.json" \
      ${BASELINE:+--baseline "$BASELINE"}
  GATE=$?
  set -e

  # ---- optional AI-assisted analysis (ADVISORY: never changes the gate result; works offline without an API key)
  if [[ "${RUN_AI_ANALYSIS:-false}" == "true" ]]; then
    INFLUX_URL=$(grep -hE '^influx_url=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)
    python3 ai/analyze_run.py --results "$RESULTS_DIR" --profile "$PROFILE_FILE" --thresholds thresholds/thresholds.json \
        --skip-sec "${RAMP:-0}" --out "$RESULTS_DIR/ai" ${BASELINE:+--baseline "$BASELINE"} \
        ${ANNOTATE_GRAFANA:+--influx-url "$INFLUX_URL" --application "omp_${TESTRUN}"} \
        || echo "[ai] analysis skipped (non-fatal)" >&2
  fi
  # ---- keep passing runs as history for ai/learn_thresholds.py
  if [[ "${SAVE_HISTORY:-false}" == "true" && $GATE -ne 1 ]]; then
    mkdir -p baseline/history && cp "$RESULTS_DIR/summary.json" "baseline/history/${TESTRUN}.json"
  fi
  exit $GATE   # 0 pass | 1 SLA breach | 2 regression warning
fi
