#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_NAME="YiYiOpenDataBox--local-yiyi--SkillRemaining91"
RUN_DIR="output/${RUN_NAME}"
CONTAINER_NAME="${1:-wb-skillremaining91}"
MODEL="${JUDGE_MODEL:-deepseek-v4-pro-guan-cc}"
LOG_DIR="logs/skill_runs"
LOG_FILE="${AUTO_JUDGE_LOG_FILE:-${LOG_DIR}/skillremaining91_auto_judge.log}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
if ! touch "$LOG_FILE" 2>/dev/null; then
  LOG_FILE="/tmp/skillremaining91_auto_judge.log"
  touch "$LOG_FILE"
fi

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE"
}

is_running() {
  docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q '^true$'
}

log "watching container: ${CONTAINER_NAME}"
while is_running; do
  done_count="$(find "$RUN_DIR" -maxdepth 2 -name agent.json 2>/dev/null | wc -l | tr -d ' ')"
  log "agent run still active; completed agent.json: ${done_count}/91"
  sleep 120
done

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  status="$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  log "agent container finished: ${status}"
else
  log "agent container not found; continuing if run output exists"
fi

if [[ ! -d "$RUN_DIR" ]]; then
  log "run dir missing: ${RUN_DIR}"
  exit 1
fi

done_count="$(find "$RUN_DIR" -maxdepth 2 -name agent.json 2>/dev/null | wc -l | tr -d ' ')"
log "starting agent-as-a-judge for ${RUN_DIR}; completed agent.json: ${done_count}/91"

docker compose -f docker/docker-compose.yaml run --rm workspace-bench bash -lc \
  "cd /workspace/Workspace-Bench/evaluation && python3 -u src/agent_as_a_judge.py --task-dir '${RUN_DIR}' --eval-yaml runs/judge.yaml --parallel --workers 5 --max-retries 3 --overwrite" \
  2>&1 | tee -a "$LOG_FILE"

log "refreshing strict output-path rubrics"
docker compose -f docker/docker-compose.yaml run --rm workspace-bench bash -lc \
  "cd /workspace/Workspace-Bench/evaluation && python3 -u scripts/refresh_path_rubric_results.py --run-dir '${RUN_DIR}' --eval-yaml runs/judge.yaml --model '${MODEL}' --refresh-only --workers 8" \
  2>&1 | tee -a "$LOG_FILE"

log "summarizing judge rubrics"
SUMMARY_JSON="${AUTO_JUDGE_SUMMARY_JSON:-${LOG_DIR}/skillremaining91_judge_summary.json}"
if ! touch "$SUMMARY_JSON" 2>/dev/null; then
  SUMMARY_JSON="/tmp/skillremaining91_judge_summary.json"
fi
python3 scripts/summarize_judge_rubrics.py "$RUN_DIR" --model "$MODEL" --json > "$SUMMARY_JSON"
python3 scripts/summarize_judge_rubrics.py "$RUN_DIR" --model "$MODEL" --no-details 2>&1 | tee -a "$LOG_FILE"

log "auto judge complete"
