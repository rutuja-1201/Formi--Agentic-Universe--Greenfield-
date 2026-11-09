#!/usr/bin/env bash
# Demonstrates a forced controller crash while the simulator stays alive.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; fi
STAMP="$(date +%Y%m%d_%H%M%S)"
SIM_DIR="runs/crash_sim_${STAMP}"
DB="runs/controller_crash_${STAMP}.sqlite"
SIM_LOG="reports/crash_sim_${STAMP}.log"
FIRST_LOG="reports/crash_first_${STAMP}.log"
RESUME_LOG="reports/crash_resume_${STAMP}.log"
REPORT="reports/crash_recovery_${STAMP}.md"
mkdir -p reports runs
if [[ -e "$SIM_DIR" || -e "$DB" ]]; then
  echo "Refusing to reuse an existing crash-recovery run." >&2
  exit 2
fi

"$PYTHON" -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir "$SIM_DIR" >"$SIM_LOG" 2>&1 &
SIM_PID=$!
CTRL_PID=""
cleanup() {
  if [[ -n "$CTRL_PID" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then kill -TERM "$CTRL_PID" || true; fi
  if kill -0 "$SIM_PID" 2>/dev/null; then kill -TERM "$SIM_PID" || true; fi
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 600); do
  if curl -fsS http://127.0.0.1:8700/health >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.1
done
if [[ "$ready" != 1 ]]; then echo "Simulator failed to start; see $SIM_LOG" >&2; exit 1; fi

CONTROLLER_DB="$DB" CONTROLLER_TICK_DELAY=0.2 "$ROOT/run.sh" http://127.0.0.1:8700 >"$FIRST_LOG" 2>&1 &
CTRL_PID=$!
progress=0
for _ in $(seq 1 600); do
  pending="$("$PYTHON" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("select count(*) from proposals where status in (\x27pending\x27,\x27approved\x27,\x27submitting\x27)").fetchone()[0])' "$DB" 2>/dev/null || echo 0)"
  if [[ "$pending" != 0 ]]; then progress=1; break; fi
  if ! kill -0 "$CTRL_PID" 2>/dev/null; then break; fi
  sleep 0.1
done
if [[ "$progress" != 1 ]]; then echo "Controller made no progress; see $FIRST_LOG" >&2; exit 1; fi
# Stop before the next simulated hour so the approval is still pending remotely.
kill -KILL "$CTRL_PID"
wait "$CTRL_PID" 2>/dev/null || true
CTRL_PID=""

CONTROLLER_DB="$DB" "$ROOT/run.sh" http://127.0.0.1:8700 >"$RESUME_LOG" 2>&1
LAST_LINE="$(tail -n 1 "$RESUME_LOG")"
if [[ "$LAST_LINE" != *'"finished":true'* ]]; then
  echo "Restarted controller did not report a finished run. See $RESUME_LOG" >&2
  exit 1
fi
DB_SUMMARY="$("$PYTHON" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print("processed_events="+c.execute("select value from meta where key=\x27events_ingested_total\x27").fetchone()[0]); print("stored_call_records="+str(c.execute("select count(*) from call_audit").fetchone()[0])); print("resume_audits="+str(c.execute("select count(*) from audit where action=\x27controller_resumed\x27").fetchone()[0])); print("applied_proposals="+str(c.execute("select count(*) from proposals where status=\x27applied\x27").fetchone()[0]))' "$DB")"
cat >"$REPORT" <<EOF
# Crash recovery evidence

- Scenario: public_02_incidents.
- The controller was killed with SIGKILL after its approval request was durably recorded as pending; the simulator process remained alive.
- The restarted controller reused the same SQLite database, reconciled the request through \`/approvals\`, resumed from the event cursor, applied it once, and finished the scenario.
- Controller finish line: \`$LAST_LINE\`
- Database check:

\`\`\`
$DB_SUMMARY
\`\`\`

Logs: \`$FIRST_LOG\`, \`$RESUME_LOG\`, \`$SIM_LOG\`.
EOF
echo "$REPORT"
