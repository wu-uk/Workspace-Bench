#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"
OPENDATABOX_ROOT="${YIYI_OPENDATABOX_PROJECT_ROOT:-${OPENDATABOX_PROJECT_ROOT:-/workspace/OpenDataBox}}"
YIYI_CARGO_ROOT="${YIYI_CARGO_ROOT:-$OPENDATABOX_ROOT/YiYi/app/src-tauri}"
YIYI_TARGET_DIR="${YIYI_DOCKER_TARGET_DIR:-/workspace/.cargo-target/yiyi}"
YIYI_EVAL_BIN="${YIYI_EVAL_BIN:-$YIYI_TARGET_DIR/debug/eval}"
DATA_ROOT="${WORKSPACE_BENCH_EXTERNAL_DATA_ROOT:-$EVAL_ROOT}"
CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"

DATASET="${YIYI_DATASET:-smoke}"
MODEL_NAME="${YIYI_MODEL_NAME:-local-yiyi}"
RUN_NAME="${YIYI_RUN_NAME:-}"
REUSE_WORKDIR_SUFFIX="${YIYI_WORKDIR_SUFFIX:-}"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dataset)
      DATASET="${2:?missing value for --dataset}"
      shift 2
      ;;
    --model)
      MODEL_NAME="${2:?missing value for --model}"
      shift 2
      ;;
    --run-name)
      RUN_NAME="${2:?missing value for --run-name}"
      shift 2
      ;;
    --data-root)
      DATA_ROOT="${2:?missing value for --data-root}"
      shift 2
      ;;
    --reuse-workdir-suffix)
      REUSE_WORKDIR_SUFFIX="${2:?missing value for --reuse-workdir-suffix}"
      shift 2
      ;;
    *)
      echo "[error] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$DATASET" in
  smoke|lite|full) ;;
  *)
    echo "[error] unsupported dataset: $DATASET" >&2
    exit 2
    ;;
esac

if [[ -z "$RUN_NAME" ]]; then
  case "$DATASET" in
    smoke) RUN_NAME="Smoke" ;;
    lite) RUN_NAME="Lite" ;;
    full) RUN_NAME="Full" ;;
  esac
fi

TASK_DIR="tasks_lite"
if [[ "$DATASET" == "full" ]]; then
  TASK_DIR="tasks"
fi

if [[ ! -d "$OPENDATABOX_ROOT" ]]; then
  echo "[error] OpenDataBox root not found: $OPENDATABOX_ROOT" >&2
  exit 1
fi

if [[ ! -d "$DATA_ROOT/$TASK_DIR" ]]; then
  echo "[error] Workspace-Bench $TASK_DIR not found under data root: $DATA_ROOT" >&2
  exit 1
fi

if [[ ! -d "$DATA_ROOT/filesys" ]]; then
  echo "[error] Workspace-Bench filesys not found under data root: $DATA_ROOT" >&2
  exit 1
fi

echo "[yiyi] incrementally building eval binary"
cd "$YIYI_CARGO_ROOT"
cargo build \
  -j "$CARGO_BUILD_JOBS" \
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
RUN_CONFIG_ARGS=(
  --eval-root "$EVAL_ROOT"
  --data-root "$DATA_ROOT"
  --harness yiyi-opendatabox
  --model "$MODEL_NAME"
  --dataset "$DATASET"
  --run-name "$RUN_NAME"
)
if [[ -n "$REUSE_WORKDIR_SUFFIX" ]]; then
  RUN_CONFIG_ARGS+=(--reuse-workdir-suffix "$REUSE_WORKDIR_SUFFIX")
fi
RUN_CONFIG="$(python3 scripts/build_run_config.py "${RUN_CONFIG_ARGS[@]}")"

echo "[yiyi] run config: $RUN_CONFIG"
PREPARE_WORKDIRS="${WORKSPACE_BENCH_PREPARE_WORKDIRS:-${RIP_BENCH_PREPARE_WORKDIRS:-auto}}"
case "$PREPARE_WORKDIRS" in
  1|true|yes|on|always)
    echo "[yiyi] preparing Workspace-Bench workdirs from raw filesystem"
    python3 scripts/prepare_workdirs_for_run.py --run-config "$RUN_CONFIG"
    ;;
  0|false|no|off|never)
    echo "[yiyi] skipping Workspace-Bench workdir preparation"
    ;;
  auto|"")
    if python3 - "$RUN_CONFIG" <<'PY'
import json
import os
import sys
from pathlib import Path

import yaml

cfg = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
fs_map = Path(cfg["fs_map_file"])
if not fs_map.is_absolute():
    fs_map = Path.cwd() / fs_map
obj = json.loads(fs_map.read_text(encoding="utf-8"))
missing = []
for role, value in obj.get("work_dir", {}).items():
    workdir = Path(value)
    if not workdir.is_dir():
        missing.append(f"{role}: missing {workdir}")
        continue
    has_files = False
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in {"model_output", ".git", "node_modules"}]
        if files:
            has_files = True
            break
    if not has_files:
        missing.append(f"{role}: empty {workdir}")
if missing:
    print("\n".join(missing), file=sys.stderr)
    sys.exit(1)
PY
    then
      echo "[yiyi] existing Workspace-Bench workdirs look prepared"
    else
      echo "[yiyi] preparing Workspace-Bench workdirs from raw filesystem"
      python3 scripts/prepare_workdirs_for_run.py --run-config "$RUN_CONFIG"
    fi
    ;;
  *)
    echo "[error] unsupported WORKSPACE_BENCH_PREPARE_WORKDIRS/RIP_BENCH_PREPARE_WORKDIRS: $PREPARE_WORKDIRS" >&2
    exit 2
    ;;
esac
python3 -u src/agent_runner.py --run-config "$RUN_CONFIG"

REPORT="output/YiYiOpenDataBox--${MODEL_NAME}--${RUN_NAME}/agent_runner_report.json"
if [[ -f "$REPORT" ]]; then
  python3 scripts/assert_agent_runner_report.py "$REPORT"
  echo "[yiyi] report: $EVAL_ROOT/$REPORT"
fi
