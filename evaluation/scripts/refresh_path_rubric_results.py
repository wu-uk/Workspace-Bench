#!/usr/bin/env python3
"""Classify output-path rubrics with the judge LLM and refresh judge results.

This script intentionally uses the same ClaudeCode/Anthropic-compatible path as
agent_as_a_judge.py. It does not call agent_eval.py and does not rewrite
baseUrl values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


EVAL_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVAL_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent_as_a_judge as _aaj  # noqa: E402
from agents import claudecode as _claudecode  # noqa: E402


Json = Any

DEFAULT_CACHE_DIR = EVAL_ROOT / ".generated" / "path_rubric_classifications"
HOST_REPO_PREFIX = Path("/home/yukai/project/OpenDataBox")
CONTAINER_REPO_PREFIX = Path("/workspace/OpenDataBox")

STRICT_PATH_INCLUDE_PATTERNS = [
    "saved under",
    "saved in",
    "saved to",
    "stored under",
    "stored in",
    "stored according to",
    "placed in",
    "placed under",
    "located in",
    "located under",
    "under the",
    "inside the",
    "root folder",
    "root directory",
    "subfolder",
    "sub-folder",
    "directory created",
    "folder created",
    "created successfully",
    "created in",
    "generated and saved",
    "generated and placed",
    "output path",
    "output directory",
    "output folder",
    "file name",
    "filename",
    "named exactly",
    "exactly named",
    "extension",
    "suffix",
    "archive contain",
    "folder contain",
    "directory contain",
    "contain the file",
    "contains the file",
    "备份",
    "子目录",
    "目录",
    "文件夹",
    "路径",
    "文件名",
    "后缀",
]

STRICT_CONTENT_EXCLUDE_PATTERNS = [
    "size",
    "bytes",
    "worksheet",
    "sheet",
    "cell",
    "row",
    "column",
    "table",
    "chart",
    "figure",
    "title",
    "abstract",
    "summary",
    "state that",
    "describe",
    "include",
    "contain the complete",
    "contain a complete",
    "contain 10",
    "contain 7",
    "contain 6",
    "contain 5",
    "contain 4",
    "contains 10",
    "contains 7",
    "contains 6",
    "contains 5",
    "contains 4",
    "number of",
    "average",
    "total monthly",
    "rate",
    "amount",
    "score",
    "chapter",
    "article",
    "module",
    "subsection",
    "section",
    "checklist",
    "list all",
    "correctly list",
    "risk",
    "to-do",
    "todo",
    "records",
    "activity",
    "only the files",
    "specified sources",
    "unrelated files",
    "from the specified",
    "来源",
    "数据",
    "数值",
    "金额",
    "平均",
    "总数",
    "章节",
    "条款",
    "工作表",
    "标题",
    "内容",
    "清单",
    "风险",
    "摘要",
]


def _read_json(path: Path) -> Json:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_rel(path: Path) -> str:
    try:
        return str(path.relative_to(EVAL_ROOT))
    except ValueError:
        return str(path)


def _json_first_object(text: str) -> Json | None:
    return _aaj._json_first_object(text)


def _load_eval_cfg(eval_yaml: Path) -> dict[str, Json]:
    cfg = _aaj._read_yaml(str(eval_yaml))
    base_url = cfg.get("baseUrl")
    model = cfg.get("model")
    api_key = cfg.get("apiKey")
    model_name = cfg.get("model_name") or model or "unknown"
    if not base_url or not model or not api_key:
        raise SystemExit(f"Missing baseUrl/model/apiKey in eval yaml: {eval_yaml}")
    return {
        "provider_type": "anthropic",
        "baseUrl": str(base_url),
        "model": str(model),
        "apiKey": str(api_key),
        "model_name": str(model_name),
    }


def _task_dirs(path: Path) -> list[Path]:
    if (path / "metadata.json").is_file():
        return [path]
    out: list[Path] = []
    for p in path.iterdir():
        if not p.name.isdigit():
            continue
        candidate = p
        if not (candidate / "metadata.json").is_file() and p.is_symlink():
            target = Path(os.readlink(p))
            if not target.is_absolute():
                target = p.parent / target
            try:
                rel = target.relative_to(HOST_REPO_PREFIX)
                mapped = CONTAINER_REPO_PREFIX / rel
                if (mapped / "metadata.json").is_file():
                    candidate = mapped
            except ValueError:
                pass
        if (candidate / "metadata.json").is_file():
            out.append(candidate)
    return sorted(out, key=lambda p: int(p.name))


def _latest_rubrics_path(task_dir: Path, model: str | None) -> Path | None:
    pattern = f"rubrics_judge--{model}.json" if model else "rubrics_judge--*.json"
    files = list(task_dir.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _load_task_rubrics(task_dir: Path) -> list[str]:
    meta = _read_json(task_dir / "metadata.json")
    rubrics = meta.get("rubrics") if isinstance(meta, dict) else None
    if not isinstance(rubrics, list) or not all(isinstance(x, str) for x in rubrics):
        raise ValueError(f"No string rubrics in {task_dir / 'metadata.json'}")
    return rubrics


def _is_strict_output_path_rubric(text: str) -> bool:
    """Keep only pure output path/name/location constraints.

    This intentionally rejects mixed rubrics such as "file exists in folder and
    has size X" because the adjustment policy should not mask content/quality
    failures.
    """
    low = text.lower()
    if not any(pattern in low for pattern in STRICT_PATH_INCLUDE_PATTERNS):
        return False

    # Folder/file membership rubrics often use "contain(s) the file"; keep them
    # only when they do not also ask about content, size, numerical values, etc.
    if any(pattern in low for pattern in STRICT_CONTENT_EXCLUDE_PATTERNS):
        allowed_membership = (
            ("contain the file" in low or "contains the file" in low)
            and not any(pattern in low for pattern in ["size", "bytes", "worksheet", "sheet", "content"])
        )
        allowed_count_in_folder = (
            ("folder contain" in low or "directory contain" in low)
            and "files" in low
            and not any(pattern in low for pattern in ["size", "bytes", "worksheet", "sheet", "content"])
        )
        if not (allowed_membership or allowed_count_in_folder):
            return False
    return True


def _build_classifier_prompt(task_id: str, rubrics: list[str]) -> str:
    payload = {
        "taskId": task_id,
        "instruction": [
            "Classify which rubrics contain output path constraints.",
            "output_path_related=true if the rubric requires or checks where an output artifact is saved, including directory name, folder structure, relative path, filename, file extension, exact output file name, archive folder membership, or generated folder existence.",
            "output_path_related=false for rubrics about document content, numerical correctness, chart type, formatting inside a file, file size/openability, data source choice, or analysis quality when no output location/name constraint is involved.",
            "If a rubric mixes path/name constraints with other checks, set output_path_related=true because path mistakes should be ignored by this adjustment policy.",
            "Return only JSON with this shape: {\"items\":[{\"index\":0,\"output_path_related\":true,\"reason\":\"...\"}]}",
        ],
        "rubrics": [{"index": i, "text": r} for i, r in enumerate(rubrics)],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def classify_task(
    *,
    task_dir: Path,
    eval_provider: dict[str, Json],
    cache_dir: Path,
    overwrite: bool,
    timeout_s: float,
) -> dict[str, Json]:
    task_id = task_dir.name
    rubrics = _load_task_rubrics(task_dir)
    out_path = cache_dir / f"{task_id}.json"
    if out_path.exists() and not overwrite:
        cached = _read_json(out_path)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            return cached

    sandbox_dir = cache_dir / "_raw" / task_id / str(int(time.time()))
    work_dir = sandbox_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt = _build_classifier_prompt(task_id, rubrics)
    run_out = _claudecode.run(
        prompt="You are a strict rubric classifier. Return only JSON.\n\n" + prompt,
        work_dir=str(work_dir),
        sandbox_dir=str(sandbox_dir),
        timeout_s=timeout_s,
        api_provider=eval_provider,
        agent_id="ClaudeCode.js",
    )

    trace = run_out.get("trace") if isinstance(run_out, dict) else None
    last_text = trace.get("lastText") if isinstance(trace, dict) and isinstance(trace.get("lastText"), str) else ""
    obj = _json_first_object(last_text)
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        raise RuntimeError(f"Classifier output parse failed for task {task_id}: {str(run_out.get('errorMessage') or '')[:500]}")

    by_index: dict[int, dict[str, Json]] = {}
    for item in obj.get("items", []):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(rubrics):
            continue
        by_index[index] = {
            "index": index,
            "rubric": rubrics[index],
            "output_path_related": bool(item.get("output_path_related")),
            "reason": str(item.get("reason") or ""),
        }

    items = [
        by_index.get(
            i,
            {
                "index": i,
                "rubric": rubric,
                "output_path_related": False,
                "reason": "Missing classifier item; defaulted to false.",
            },
        )
        for i, rubric in enumerate(rubrics)
    ]
    result = {
        "taskId": task_id,
        "createdAt": _aaj._iso_now(),
        "classifier": {
            "model": eval_provider.get("model"),
            "modelName": eval_provider.get("model_name"),
            "baseUrl": eval_provider.get("baseUrl"),
            "status": run_out.get("status") if isinstance(run_out, dict) else None,
        },
        "items": items,
    }
    _write_json(out_path, result)
    return result


def refresh_task_result(
    *,
    task_dir: Path,
    classification: dict[str, Json],
    cache_dir: Path,
    model: str | None,
    dry_run: bool,
) -> dict[str, Json]:
    rubrics_path = _latest_rubrics_path(task_dir, model)
    if rubrics_path is None:
        return {"taskId": task_dir.name, "skipped": True, "reason": "rubrics_judge file not found"}

    data = _read_json(rubrics_path)
    rubrics = data.get("rubrics") if isinstance(data, dict) else None
    if not isinstance(rubrics, list):
        return {"taskId": task_dir.name, "skipped": True, "reason": "rubrics list not found"}

    path_indices: set[int] = set()
    for item in classification.get("items", []):
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        rubric_text = str(item.get("rubric") or "")
        if _is_strict_output_path_rubric(rubric_text):
            path_indices.add(int(item["index"]))

    adjusted = 0
    adjusted_indices: list[int] = []
    for item in rubrics:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or index not in path_indices:
            continue
        if item.get("passed") is True:
            continue
        item["originalPassed"] = item.get("passed")
        item["passed"] = True
        item["adjustedByOutputPathRubric"] = True
        item["adjustmentReason"] = (
            "This rubric contains an output path/name/location constraint. "
            "The experiment uses a unified target output directory, so path-related failures are counted as passed."
        )
        item["originalEvidence"] = item.get("evidence", "")
        item["evidence"] = f"[ADJUSTED OUTPUT PATH RUBRIC] {item.get('evidence', '')}"
        adjusted += 1
        adjusted_indices.append(index)

    passed_n = len([x for x in rubrics if isinstance(x, dict) and x.get("passed") is True])
    total_n = len([x for x in rubrics if isinstance(x, dict)])
    if isinstance(data, dict):
        data["summary"] = {"total": total_n, "passed": passed_n, "failed": total_n - passed_n}
        data["outputPathRubricAdjustment"] = {
            "updatedAt": _aaj._iso_now(),
            "classificationPath": _safe_rel(cache_dir / f"{task_dir.name}.json")
            if (cache_dir / f"{task_dir.name}.json").exists()
            else None,
            "pathRelatedRubricIndices": sorted(path_indices),
            "adjustedFailedRubricIndices": sorted(adjusted_indices),
            "adjustedCount": adjusted,
        }

    if adjusted and not dry_run:
        backup = rubrics_path.with_suffix(rubrics_path.suffix + ".before_path_adjustment")
        if not backup.exists():
            backup.write_text(rubrics_path.read_text(encoding="utf-8"), encoding="utf-8")
        _write_json(rubrics_path, data)

    return {
        "taskId": task_dir.name,
        "rubricsPath": _safe_rel(rubrics_path),
        "pathRelatedRubrics": len(path_indices),
        "adjusted": adjusted,
        "summary": data.get("summary") if isinstance(data, dict) else None,
        "dryRun": dry_run,
    }


def restore_task_backup(*, task_dir: Path, model: str | None, dry_run: bool) -> dict[str, Json]:
    rubrics_path = _latest_rubrics_path(task_dir, model)
    if rubrics_path is None:
        return {"taskId": task_dir.name, "skipped": True, "reason": "rubrics_judge file not found"}
    backup = rubrics_path.with_suffix(rubrics_path.suffix + ".before_path_adjustment")
    if not backup.exists():
        return {"taskId": task_dir.name, "restored": False, "reason": "backup not found"}
    if not dry_run:
        rubrics_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
    return {"taskId": task_dir.name, "restored": True, "rubricsPath": _safe_rel(rubrics_path), "dryRun": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify output-path rubrics using agent-as-a-judge LLM config and refresh rubrics_judge results."
    )
    parser.add_argument("--run-dir", required=True, help="Run directory or single task directory")
    parser.add_argument("--eval-yaml", required=True, help="Judge YAML used by agent_as_a_judge.py")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--model", help="Only refresh rubrics_judge--<model>.json")
    parser.add_argument("--overwrite-classification", action="store_true")
    parser.add_argument("--classify-only", action="store_true")
    parser.add_argument("--refresh-only", action="store_true")
    parser.add_argument("--restore-backups", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=1, help="Number of tasks to classify/refresh in parallel")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = EVAL_ROOT / run_dir
    eval_yaml = Path(args.eval_yaml)
    if not eval_yaml.is_absolute():
        eval_yaml = EVAL_ROOT / eval_yaml
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = EVAL_ROOT / cache_dir

    tasks = _task_dirs(run_dir)
    if not tasks:
        raise SystemExit(f"No task directories found under {run_dir}")

    provider = _load_eval_cfg(eval_yaml)
    def process_one(task_dir: Path) -> dict[str, Json]:
        if args.restore_backups:
            return restore_task_backup(task_dir=task_dir, model=args.model, dry_run=args.dry_run)

        classification_path = cache_dir / f"{task_dir.name}.json"
        if args.refresh_only:
            if not classification_path.exists():
                raise SystemExit(f"Missing cached classification: {classification_path}")
            classification = _read_json(classification_path)
        else:
            classification = classify_task(
                task_dir=task_dir,
                eval_provider=provider,
                cache_dir=cache_dir,
                overwrite=args.overwrite_classification,
                timeout_s=args.timeout_sec,
            )

        if args.classify_only:
            return {"taskId": task_dir.name, "classificationPath": _safe_rel(classification_path)}

        return refresh_task_result(
            task_dir=task_dir,
            classification=classification,
            cache_dir=cache_dir,
            model=args.model,
            dry_run=args.dry_run,
        )

    results: list[dict[str, Json]] = []
    if args.workers <= 1 or len(tasks) <= 1:
        for task_dir in tasks:
            results.append(process_one(task_dir))
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_to_task = {executor.submit(process_one, task_dir): task_dir for task_dir in tasks}
            for future in as_completed(future_to_task):
                task_dir = future_to_task[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"taskId": task_dir.name, "error": f"{type(exc).__name__}: {exc}"})

    results.sort(key=lambda row: int(str(row.get("taskId"))) if str(row.get("taskId", "")).isdigit() else 10**9)
    summary = {
        "tasks": len(results),
        "adjusted": sum(int(r.get("adjusted") or 0) for r in results if isinstance(r, dict)),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
