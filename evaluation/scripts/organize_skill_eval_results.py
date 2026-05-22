#!/usr/bin/env python3
import csv
import json
from pathlib import Path
from typing import Any


Json = Any

EVAL_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = EVAL_ROOT / "output"
SUMMARY_ROOT = EVAL_ROOT / ".generated" / "skill_eval_summary"

BASELINE_RUN = "YiYiOpenDataBox--local-yiyi--Ten3PhaseFixClean100"
SKILL_RUNS = [
    "YiYiOpenDataBox--local-yiyi--Skill311Phase3",
    "YiYiOpenDataBox--local-yiyi--SkillMini3",
    "YiYiOpenDataBox--local-yiyi--SkillMore5",
    "YiYiOpenDataBox--local-yiyi--SkillPptFix381",
]
EXTRA_SKILL_RUNS = [
    "YiYiOpenDataBox--local-yiyi--SkillPptFixNew4",
]


def _load_json(path: Path) -> Json | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_rubrics(task_dir: Path) -> Path | None:
    files = list(task_dir.glob("rubrics_judge--*.json"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _score_task(task_dir: Path) -> dict[str, Json] | None:
    rubrics_path = _latest_rubrics(task_dir)
    if rubrics_path is None:
        return None
    data = _load_json(rubrics_path)
    if not isinstance(data, dict):
        return None
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    rubrics = data.get("rubrics")
    if isinstance(summary, dict) and "passed" in summary and "total" in summary:
        passed = int(summary.get("passed") or 0)
        total = int(summary.get("total") or 0)
    elif isinstance(rubrics, list):
        passed = sum(1 for r in rubrics if isinstance(r, dict) and bool(r.get("passed")))
        total = len([r for r in rubrics if isinstance(r, dict)])
    else:
        return None
    return {
        "passed": passed,
        "total": total,
        "rate": passed / total if total else 0.0,
        "rubricsPath": str(rubrics_path.relative_to(EVAL_ROOT)),
        "judgeKind": "agent_as_a_judge" if (task_dir / "raw" / "agent_as_a_judge").exists() else "unknown",
    }


def _load_rubric_items(task_dir: Path) -> list[dict[str, Json]]:
    rubrics_path = _latest_rubrics(task_dir)
    if rubrics_path is None:
        return []
    data = _load_json(rubrics_path)
    if not isinstance(data, dict):
        return []
    rubrics = data.get("rubrics")
    if not isinstance(rubrics, list):
        return []
    return [r for r in rubrics if isinstance(r, dict)]


def _agent_status(task_dir: Path) -> str | None:
    data = _load_json(task_dir / "agent.json")
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, str):
            return status
    return None


def _run_rows(run_name: str, role: str) -> list[dict[str, Json]]:
    run_dir = OUTPUT_ROOT / run_name
    rows: list[dict[str, Json]] = []
    if not run_dir.is_dir():
        return rows
    for task_dir in sorted(
        [p for p in run_dir.iterdir() if p.is_dir() and p.name.isdigit()],
        key=lambda p: int(p.name),
    ):
        score = _score_task(task_dir)
        row: dict[str, Json] = {
            "run": run_name,
            "role": role,
            "taskId": task_dir.name,
            "agentStatus": _agent_status(task_dir),
            "passed": None,
            "total": None,
            "rate": None,
            "judgeKind": None,
            "rubricsPath": None,
        }
        if score:
            row.update(score)
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict[str, Json]]) -> None:
    fields = [
        "run",
        "role",
        "taskId",
        "agentStatus",
        "passed",
        "total",
        "rate",
        "judgeKind",
        "rubricsPath",
        "baselinePassed",
        "baselineTotal",
        "skillPassed",
        "skillTotal",
        "deltaPassed",
        "sourceSkillRun",
        "outputPathAdjustedRubrics",
        "rubricIndex",
        "rubric",
        "evidence",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    SUMMARY_ROOT.mkdir(parents=True, exist_ok=True)

    baseline_rows = _run_rows(BASELINE_RUN, "baseline")
    skill_rows: list[dict[str, Json]] = []
    for run in SKILL_RUNS:
        skill_rows.extend(_run_rows(run, "skill_validated"))
    for run in EXTRA_SKILL_RUNS:
        skill_rows.extend(_run_rows(run, "skill_unjudged"))

    baseline_by_task = {
        str(row["taskId"]): row
        for row in baseline_rows
        if isinstance(row.get("passed"), int) and isinstance(row.get("total"), int)
    }
    output_path_adjustments: list[dict[str, Json]] = []
    output_path_adjusted_by_task: dict[str, int] = {}
    for task_id in sorted(baseline_by_task, key=lambda x: int(x)):
        task_dir = OUTPUT_ROOT / BASELINE_RUN / task_id
        for rubric in _load_rubric_items(task_dir):
            if rubric.get("adjustedByOutputPathRubric") is True:
                output_path_adjusted_by_task[task_id] = output_path_adjusted_by_task.get(task_id, 0) + 1
                output_path_adjustments.append(
                    {
                        "taskId": task_id,
                        "rubricIndex": rubric.get("index"),
                        "rubric": rubric.get("rubric"),
                        "evidence": rubric.get("evidence"),
                    }
                )

    skill_by_task: dict[str, dict[str, Json]] = {}
    for row in skill_rows:
        if row.get("role") != "skill_validated":
            continue
        if not isinstance(row.get("passed"), int) or not isinstance(row.get("total"), int):
            continue
        skill_by_task[str(row["taskId"])] = row

    overlay_rows: list[dict[str, Json]] = []
    baseline_passed = sum(int(row["passed"]) for row in baseline_by_task.values())
    baseline_total = sum(int(row["total"]) for row in baseline_by_task.values())
    overlay_passed = baseline_passed
    for task_id in sorted(set(baseline_by_task) & set(skill_by_task), key=lambda x: int(x)):
        base = baseline_by_task[task_id]
        skill = skill_by_task[task_id]
        row = {
            "run": "baseline_skill_overlay",
            "role": "overlay",
            "taskId": task_id,
            "baselinePassed": base["passed"],
            "baselineTotal": base["total"],
            "skillPassed": skill["passed"],
            "skillTotal": skill["total"],
            "deltaPassed": None,
            "sourceSkillRun": skill["run"],
            "rubricsPath": skill["rubricsPath"],
            "outputPathAdjustedRubrics": output_path_adjusted_by_task.get(task_id, 0),
        }
        if base["total"] == skill["total"]:
            delta = int(skill["passed"]) - int(base["passed"])
            row["deltaPassed"] = delta
            overlay_passed += delta
        overlay_rows.append(row)

    summary = {
        "baselineRun": BASELINE_RUN,
        "baselineTasks": len(baseline_by_task),
        "baselinePassed": baseline_passed,
        "baselineTotal": baseline_total,
        "baselineRate": baseline_passed / baseline_total if baseline_total else 0.0,
        "outputPathAdjustedRubrics": sum(output_path_adjusted_by_task.values()),
        "outputPathAdjustedTasks": len(output_path_adjusted_by_task),
        "validatedSkillRuns": SKILL_RUNS,
        "extraSkillRunsWithoutRubrics": EXTRA_SKILL_RUNS,
        "validatedSkillExecutions": len([r for r in skill_rows if r.get("role") == "skill_validated"]),
        "overlayCoveredTasks": len(overlay_rows),
        "overlayPassed": overlay_passed,
        "overlayTotal": baseline_total,
        "overlayRate": overlay_passed / baseline_total if baseline_total else 0.0,
        "overlayDeltaPassed": overlay_passed - baseline_passed,
        "overlayDeltaRate": (overlay_passed - baseline_passed) / baseline_total if baseline_total else 0.0,
    }

    all_rows = baseline_rows + skill_rows
    (SUMMARY_ROOT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SUMMARY_ROOT / "all_runs.json").write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SUMMARY_ROOT / "overlay.json").write_text(
        json.dumps(overlay_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SUMMARY_ROOT / "path_adjustments.json").write_text(
        json.dumps(output_path_adjustments, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(SUMMARY_ROOT / "all_runs.csv", all_rows)
    _write_csv(SUMMARY_ROOT / "overlay.csv", overlay_rows)
    _write_csv(SUMMARY_ROOT / "path_adjustments.csv", output_path_adjustments)

    md = [
        "# Skill Eval Summary",
        "",
        f"- Baseline run: `{BASELINE_RUN}`",
        f"- Baseline: `{baseline_passed}/{baseline_total}` ({summary['baselineRate'] * 100:.2f}%)",
        f"- Output-path rubrics adjusted by LLM refresh: `{summary['outputPathAdjustedRubrics']}` across `{summary['outputPathAdjustedTasks']}` tasks",
        f"- Strict overlay: `{overlay_passed}/{baseline_total}` ({summary['overlayRate'] * 100:.2f}%)",
        f"- Delta: `{overlay_passed - baseline_passed}` rubrics ({summary['overlayDeltaRate'] * 100:.2f} percentage points)",
        f"- Covered baseline tasks: `{len(overlay_rows)}`",
        "",
        "## Overlay Rows",
        "",
        "| task | baseline | skill | delta | source |",
        "|---|---:|---:|---:|---|",
    ]
    for row in overlay_rows:
        md.append(
            f"| {row['taskId']} | {row['baselinePassed']}/{row['baselineTotal']} | "
            f"{row['skillPassed']}/{row['skillTotal']} | {row['deltaPassed']} | `{row['sourceSkillRun']}` |"
        )
    (SUMMARY_ROOT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
