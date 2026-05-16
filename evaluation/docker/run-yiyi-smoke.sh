#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"
OPENDATABOX_ROOT="${YIYI_OPENDATABOX_PROJECT_ROOT:-${OPENDATABOX_PROJECT_ROOT:-/workspace/OpenDataBox}}"
YIYI_CARGO_ROOT="${YIYI_CARGO_ROOT:-$OPENDATABOX_ROOT/YiYi/app/src-tauri}"
YIYI_TARGET_DIR="${YIYI_DOCKER_TARGET_DIR:-/workspace/.cargo-target/yiyi}"
YIYI_EVAL_BIN="${YIYI_EVAL_BIN:-$YIYI_TARGET_DIR/debug/eval}"
DATA_ROOT="${WORKSPACE_BENCH_EXTERNAL_DATA_ROOT:-$EVAL_ROOT}"
REUSE_WORKDIR_SUFFIX="${YIYI_WORKDIR_SUFFIX:-Codex_GPT-5.4}"
MODEL_NAME="${YIYI_MODEL_NAME:-local-yiyi}"
RUN_NAME="${YIYI_RUN_NAME:-Smoke}"

if [[ ! -d "$OPENDATABOX_ROOT" ]]; then
  echo "[error] OpenDataBox root not found: $OPENDATABOX_ROOT" >&2
  exit 1
fi

if [[ ! -d "$DATA_ROOT/tasks_lite" ]]; then
  echo "[error] Workspace-Bench tasks_lite not found under data root: $DATA_ROOT" >&2
  exit 1
fi

if [[ ! -d "$DATA_ROOT/filesys" ]]; then
  echo "[error] Workspace-Bench filesys not found under data root: $DATA_ROOT" >&2
  exit 1
fi

echo "[yiyi] building eval binary"
cd "$YIYI_CARGO_ROOT"
cargo build \
  --bin eval \
  --no-default-features \
  --features memme-core/bundled \
  --target-dir "$YIYI_TARGET_DIR"

if [[ ! -x "$YIYI_EVAL_BIN" ]]; then
  echo "[error] eval binary not found after build: $YIYI_EVAL_BIN" >&2
  exit 1
fi

echo "[yiyi] generating Workspace-Bench run config"
cd "$EVAL_ROOT"
RUN_CONFIG="$(python3 scripts/build_run_config.py \
  --eval-root "$EVAL_ROOT" \
  --data-root "$DATA_ROOT" \
  --harness yiyi-opendatabox \
  --model "$MODEL_NAME" \
  --dataset smoke \
  --run-name "$RUN_NAME" \
  --reuse-workdir-suffix "$REUSE_WORKDIR_SUFFIX")"

echo "[yiyi] run config: $RUN_CONFIG"
python3 -u src/agent_runner.py --run-config "$RUN_CONFIG"

REPORT="output/YiYiOpenDataBox--${MODEL_NAME}--${RUN_NAME}/agent_runner_report.json"
if [[ -f "$REPORT" ]]; then
  python3 scripts/assert_agent_runner_report.py "$REPORT"
  echo "[yiyi] report: $EVAL_ROOT/$REPORT"
fi
