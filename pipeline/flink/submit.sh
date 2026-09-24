#!/usr/bin/env bash
# Submit the PagedOut ingestion job to the local Flink cluster.
#
# Cancels any job already running first. The cluster has 4 slots; submitting
# twice silently starves both copies and produces a job stuck in RESTARTING,
# which is an unpleasant thing to debug.
#
# Usage: ./pipeline/flink/submit.sh [path/to/job.sql]

set -euo pipefail

JOB_SQL="${1:-/opt/pagedout/jobs/incident_pipeline.sql}"
JM_REST="${JM_REST:-http://localhost:8088}"
COMPOSE="docker compose"

echo "==> Cancelling any running jobs"
running=$(curl -fsS "${JM_REST}/jobs" \
  | python3 -c "import json,sys; print(' '.join(j['id'] for j in json.load(sys.stdin)['jobs'] if j['status'] in ('RUNNING','RESTARTING','CREATED')))")

if [[ -n "${running// /}" ]]; then
  for id in $running; do
    echo "    cancelling $id"
    curl -fsS -X PATCH "${JM_REST}/jobs/${id}?mode=cancel" >/dev/null || true
  done
  sleep 5
else
  echo "    none running"
fi

echo "==> Submitting ${JOB_SQL}"
output=$($COMPOSE exec -T flink-jobmanager ./bin/sql-client.sh -f "$JOB_SQL" 2>&1)

if grep -q '\[ERROR\]' <<<"$output"; then
  echo "$output" | grep -A5 '\[ERROR\]' | head -20
  exit 1
fi

job_id=$(grep -oE '[0-9a-f]{32}' <<<"$output" | head -1)
if [[ -z "$job_id" ]]; then
  echo "could not determine job id; raw output:"
  echo "$output" | tail -20
  exit 1
fi

echo "==> Job ID: $job_id"
echo "==> Waiting for RUNNING"
for _ in $(seq 1 30); do
  state=$(curl -fsS "${JM_REST}/jobs/${job_id}" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")
  if [[ "$state" == "RUNNING" ]]; then
    echo "    RUNNING"
    echo "$job_id" > /tmp/pagedout-flink-job-id
    exit 0
  fi
  if [[ "$state" == "FAILED" ]]; then
    echo "    FAILED"
    curl -fsS "${JM_REST}/jobs/${job_id}/exceptions" \
      | python3 -c "import json,sys; print((json.load(sys.stdin).get('root-exception') or '')[:800])"
    exit 1
  fi
  sleep 2
done

echo "    timed out waiting for RUNNING (last state: ${state:-unknown})"
exit 1
