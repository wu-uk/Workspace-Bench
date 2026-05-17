#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any


Json = Any


def _task_sort_key(path: Path) -> tuple[int, str]:
    name = path.parent.name
    if name.isdigit():
        return (0, f"{int(name):012d}")
    return (1, name)


def _pick_rubric_files(run_dir: Path, model: str | None) -> list[Path]:
    if model:
        return sorted(run_dir.glob(f"*/rubrics_judge--{model}.json"), key=_task_sort_key)

    by_task: dict[Path, Path] = {}
    for path in run_dir.glob("*/rubrics_judge--*.json"):
        task_dir = path.parent
        old = by_task.get(task_dir)
        if old is None or path.stat().st_mtime > old.stat().st_mtime:
            by_task[task_dir] = path
    return sorted(by_task.values(), key=_task_sort_key)


def _as_bool(value: Json) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "pass", "passed", "1"}
    return bool(value)


def _load_rubrics(path: Path) -> list[dict[str, Json]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rubrics = data.get("rubrics") if isinstance(data, dict) else data
    if isinstance(rubrics, dict):
        rubrics = rubrics.get("items") or rubrics.get("rubrics")
    if not isinstance(rubrics, list):
        raise ValueError("rubrics is not a list")
    return [r for r in rubrics if isinstance(r, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="统计 agent_as_a_judge 生成的 rubric 通过率")
    parser.add_argument("run_dir", help="评测输出目录，例如 output/YiYiOpenDataBox--local-yiyi--Ten3PhaseFix")
    parser.add_argument("--model", help="只统计指定 judge model，例如 deepseek-v4-pro-guan-cc")
    parser.add_argument("--no-details", action="store_true", help="只输出总数，不输出逐任务明细")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本消费")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"run_dir not found: {run_dir}")

    files = _pick_rubric_files(run_dir, args.model)
    rows: list[dict[str, Json]] = []
    total_passed = 0
    total_rubrics = 0
    api_error_tasks = 0
    judge_error_tasks = 0
    load_errors: list[dict[str, str]] = []

    for path in files:
        task_id = path.parent.name
        try:
            rubrics = _load_rubrics(path)
        except Exception as exc:
            load_errors.append({"taskId": task_id, "path": str(path), "error": str(exc)})
            continue

        passed = 0
        api_error = False
        judge_error = False
        for rubric in rubrics:
            evidence = str(rubric.get("evidence", ""))
            if "API Error" in evidence or "Insufficient Balance" in evidence:
                api_error = True
            if "ClaudeCode judge failed" in evidence:
                judge_error = True
            passed += int(_as_bool(rubric.get("passed")))

        count = len(rubrics)
        total_passed += passed
        total_rubrics += count
        api_error_tasks += int(api_error)
        judge_error_tasks += int(judge_error)
        model_name = path.name.removeprefix("rubrics_judge--").removesuffix(".json")
        rows.append(
            {
                "taskId": task_id,
                "model": model_name,
                "passed": passed,
                "total": count,
                "rate": (passed / count) if count else 0.0,
                "apiError": api_error,
                "judgeError": judge_error,
                "path": str(path),
            }
        )

    summary = {
        "runDir": str(run_dir),
        "completedTasks": len(rows),
        "rubricsPassed": total_passed,
        "rubricsTotal": total_rubrics,
        "rate": (total_passed / total_rubrics) if total_rubrics else 0.0,
        "apiErrorTasks": api_error_tasks,
        "judgeErrorTasks": judge_error_tasks,
        "loadErrors": load_errors,
        "tasks": rows,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    print(f"完成任务数: {summary['completedTasks']}")
    print(f"通过 rubrics: {total_passed} / {total_rubrics}")
    print(f"通过率: {summary['rate'] * 100:.2f}%")
    print(f"API 错误任务数: {api_error_tasks}")
    print(f"Judge 执行错误任务数: {judge_error_tasks}")
    if load_errors:
        print(f"读取失败文件数: {len(load_errors)}")

    if not args.no_details:
        print("")
        print("task_id\tpassed\ttotal\trate\tmodel\tflags")
        for row in rows:
            flags = []
            if row["apiError"]:
                flags.append("API_ERR")
            if row["judgeError"]:
                flags.append("JUDGE_ERR")
            print(
                f"{row['taskId']}\t{row['passed']}\t{row['total']}\t"
                f"{row['rate'] * 100:.1f}%\t{row['model']}\t{','.join(flags)}"
            )


if __name__ == "__main__":
    main()
