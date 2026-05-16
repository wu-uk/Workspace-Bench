# Workspace-Bench Evaluation

本目录是 Workspace-Bench 的统一评测入口。YiYi/OpenDataBox 已接入为 `YiYiOpenDataBox` harness，复用本仓库的 Rust eval binary：

- Rust 入口：`YiYi/app/src-tauri/src/bin/eval.rs`
- Workspace-Bench harness：`src/agents/yiyiopendatabox.py`
- 统一 runner：`src/agent_runner.py`
- Docker 冒烟脚本：`docker/run-yiyi-smoke.sh`

输出格式和 Workspace-Bench 其他 agent 保持一致：

- `output/<Agent>--<Model>--<Run>/agent_runner_report.json`
- 每个 case 的 `agent.json`
- 每个 case 的 `agent.log`
- 每个 case 的 `raw/stdout.txt`、`raw/stderr.txt`、`raw/yiyi_trace.txt`、`raw/yiyi_trace.json`
- 每个 case 的 `output/`

## 数据准备

评测需要两类数据：

- `tasks_lite/` 或 `tasks/`：任务 metadata
- `filesys/`：Workspace-Bench 文件系统

本机当前为了避免复制大文件，默认把已经下载好的数据目录映射到 Docker 内：

```bash
export WORKSPACE_BENCH_DATA_ROOT=/home/yukai/project/tmp/Workspace-Bench/evaluation
```

该目录需要包含：

```text
/home/yukai/project/tmp/Workspace-Bench/evaluation/
├── tasks_lite/
├── tasks/              # 跑 full 时需要
└── filesys/
    ├── chanpin_raw/
    ├── kaifa_raw/
    ├── research_raw/
    ├── yunying_raw/
    └── houqin_raw/
```

如果别人从 Hugging Face 重新下载，不会因为我们使用外部目录而受影响。推荐在他们自己的 `Workspace-Bench/evaluation` 目录执行：

```bash
python3 scripts/download_hf_assets.py --lite --workspaces
```

这会填充本地 `tasks_lite/` 和 `filesys/`。如果要跑 full，再执行：

```bash
python3 scripts/download_hf_assets.py --full --workspaces
```

或一次性下载：

```bash
python3 scripts/download_hf_assets.py --all
```

下载到其他目录也可以，只要运行前设置 `WORKSPACE_BENCH_DATA_ROOT=/path/to/evaluation-data`。YiYi 的 run config 会通过 `--data-root` 读取外部 `tasks_lite/tasks/filesys`，不会要求把大文件复制进当前仓库。

## YiYi 配置

YiYi harness 会读取宿主机 `~/.yiyi` 中的配置和 provider settings，并在每个 case 运行前复制到临时 `YIYI_WORKING_DIR`，避免污染桌面端真实状态。

需要确保：

- `~/.yiyi/yiyi.db` 中已有可用 provider 配置
- `~/.yiyi/models` 如存在，会以 symlink 方式复用
- 当前冒烟测试实际使用的基模由 YiYi 配置决定；`local-yiyi` 只是 Workspace-Bench 输出目录中的模型展示名

## Docker 冒烟测试

宿主机没有 sudo 或缺少 Tauri/WebKitGTK/DuckDB 动态库时，使用 Docker 路径。Docker 会在容器内编译 YiYi eval binary。

```bash
cd /home/yukai/project/OpenDataBox

export WORKSPACE_BENCH_DATA_ROOT=/home/yukai/project/tmp/Workspace-Bench/evaluation

docker compose -f Workspace-Bench/evaluation/docker/docker-compose.yaml build workspace-bench

docker compose -f Workspace-Bench/evaluation/docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-yiyi-smoke.sh
```

`run-yiyi-smoke.sh` 会执行：

1. 检查 `tasks_lite/` 和 `filesys/`
2. 在 Docker 内编译 `eval`：
   `cargo build --bin eval --no-default-features --features memme-core/bundled`
3. 生成 YiYi 的 Workspace-Bench run config
4. 调用 `src/agent_runner.py`
5. 校验 `agent_runner_report.json`

默认输出：

```text
Workspace-Bench/evaluation/output/YiYiOpenDataBox--local-yiyi--Smoke/agent_runner_report.json
```

可选环境变量：

- `YIYI_MODEL_NAME`：Workspace-Bench 输出目录中的模型展示名，默认 `local-yiyi`
- `YIYI_RUN_NAME`：run 名称，默认 `Smoke`
- `YIYI_WORKDIR_SUFFIX`：复用已有 Workspace-Bench workdir suffix，默认 `Codex_GPT-5.4`
- `YIYI_KEEP_EVAL_TMP=1`：保留每个 case 的临时 `YIYI_WORKING_DIR`

## 手动运行

如需不走冒烟脚本，可以手动生成 run config：

```bash
cd /home/yukai/project/OpenDataBox

python3 Workspace-Bench/evaluation/scripts/build_run_config.py \
  --harness yiyi-opendatabox \
  --model local-yiyi \
  --dataset smoke \
  --eval-root Workspace-Bench/evaluation \
  --data-root /home/yukai/project/tmp/Workspace-Bench/evaluation \
  --reuse-workdir-suffix Codex_GPT-5.4 \
  --run-name Smoke
```

然后运行：

```bash
cd /home/yukai/project/OpenDataBox/Workspace-Bench/evaluation

python3 -u src/agent_runner.py \
  --run-config .generated/run_configs/runs/yiyiopendatabox-local-yiyi-smoke.yaml
```

`--dataset` 可选：

- `smoke`：只跑 `tasks_lite` 中 1 个任务
- `lite`：跑 `tasks_lite`
- `full`：跑 `tasks`

## 验证结果

冒烟测试结束后检查：

```bash
python3 Workspace-Bench/evaluation/scripts/assert_agent_runner_report.py \
  Workspace-Bench/evaluation/output/YiYiOpenDataBox--local-yiyi--Smoke/agent_runner_report.json
```

也可以查看 case 级结果：

```bash
find Workspace-Bench/evaluation/output/YiYiOpenDataBox--local-yiyi--Smoke -maxdepth 4 -type f | sort
```

## 生成物

以下内容都是本地生成物，不应提交：

- `Workspace-Bench/evaluation/.generated/`
- `Workspace-Bench/evaluation/output/`
- `Workspace-Bench/evaluation/node_modules/`
- `Workspace-Bench/evaluation/filesys/*_workdir*/`
- `Workspace-Bench/evaluation/.env`

根目录 `.gitignore` 和本目录 `.gitignore` 已覆盖这些路径。
