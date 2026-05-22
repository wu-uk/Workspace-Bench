#!/usr/bin/env python3
"""Learn Phase-3 generation skill drafts from Workspace-Bench tasks."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import textwrap
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


Json = Any

DEFAULT_SOURCE_EVAL_ROOT = Path("/home/yukai/project/tmp/Workspace-Bench/evaluation")
DEFAULT_OUTPUT_DIR = Path(".generated/skill_learning")
DEFAULT_JUDGE_YAML = Path("runs/judge.yaml")

FORMAT_GROUPS = {
    "xlsx": "spreadsheet",
    "xls": "spreadsheet",
    "csv": "csv_table",
    "tsv": "csv_table",
    "pptx": "presentation",
    "ppt": "presentation",
    "docx": "document_report",
    "doc": "document_report",
    "md": "plain_markdown",
    "txt": "plain_markdown",
    "pdf": "pdf_or_visual_document",
    "png": "pdf_or_visual_document",
    "jpg": "pdf_or_visual_document",
    "jpeg": "pdf_or_visual_document",
    "html": "plain_markdown",
    "json": "code_or_structured_artifact",
    "py": "code_or_structured_artifact",
    "sh": "code_or_structured_artifact",
    "zip": "file_archive",
}

SOURCE_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,8})(?:$|[?#])")


def _read_json(path: Path) -> Json:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return re.sub(r"\$\{([^}:]+)(?::-[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, value)


def _read_simple_yaml(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"').strip("'")
        data[key.strip()] = _expand_env(value)
    return data


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_ -]+", "", value).strip().lower()
    slug = re.sub(r"[\s-]+", "_", slug)
    return slug or "cluster"


def _task_sort_key(path: Path) -> tuple[int, str]:
    name = path.parent.name
    return (0, f"{int(name):012d}") if name.isdigit() else (1, name)


def _output_ext(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or "none"


def _format_group(ext: str) -> str:
    return FORMAT_GROUPS.get(ext.lower(), "other")


def _text_has(text: str, *words: str) -> bool:
    return any(word in text for word in words)


def _extract_source_exts(meta: dict[str, Json]) -> list[str]:
    exts: set[str] = set()
    for item in meta.get("data_manifest") or []:
        if not isinstance(item, dict):
            continue
        for key in ("filename", "stored_relpath"):
            value = str(item.get(key) or "")
            suffix = Path(value).suffix.lower().lstrip(".")
            if suffix:
                exts.add(suffix)
    for edge in meta.get("file_dep_graph") or []:
        if not isinstance(edge, dict):
            continue
        for key in ("from", "to"):
            value = str(edge.get(key) or "")
            match = SOURCE_EXT_RE.search(value)
            if match:
                exts.add(match.group(1).lower())
    return sorted(exts)


def _operation_tags(task_text: str, rubric_text: str) -> list[str]:
    text = f"{task_text} {rubric_text}".lower()
    tags: list[str] = []
    checks = [
        ("compute_statistics", ("average", "median", "mean", "sum", "total", "ratio", "percentage", "year-over-year", "month-over-month", "growth rate")),
        ("rank_top_bottom", ("top", "rank", "largest", "smallest", "highest", "lowest", "least", "most")),
        ("visualize", ("chart", "visual", "plot", "dashboard", "trend chart", "bar chart", "line chart", "pie chart")),
        ("summarize", ("summary", "summarize", "overview", "briefing", "conclusion")),
        ("analyze", ("analysis", "analyze", "assessment", "evaluate", "insight")),
        ("recommend", ("recommendation", "recommend", "action plan", "follow-up", "strategy")),
        ("extract_records", ("extract", "list", "table", "all", "each", "every")),
        ("compare", ("compare", "comparison", "before and after", "difference")),
        ("classify", ("classify", "category", "categorize", "group by")),
        ("rename_archive", ("rename", "archive", "copy", "move", "delete the rest", "target directory", "output directory")),
        ("debug_code", ("bug", "debug", "fix", "code review", "vulnerability")),
    ]
    for tag, words in checks:
        if _text_has(text, *words):
            tags.append(tag)
    return tags


def _validation_surface(outputs: list[str], rubrics: list[str]) -> list[str]:
    text = " ".join(rubrics).lower()
    exts = {_output_ext(output) for output in outputs}
    surfaces = {"filename", "readable_file"}
    if "/" in " ".join(outputs) or _text_has(text, "directory", "folder", "path", "saved as", "desktop"):
        surfaces.add("path")
    if exts & {"xlsx", "xls"} or _text_has(text, "worksheet", "sheet", "spreadsheet", "excel"):
        surfaces.update({"sheet_names", "columns", "row_count", "cell_values"})
    if exts & {"csv"}:
        surfaces.update({"columns", "row_count", "delimiter", "cell_values"})
    if exts & {"pptx", "ppt"} or _text_has(text, "slide", "presentation"):
        surfaces.update({"slide_count", "slide_titles", "key_metrics"})
    if exts & {"docx", "doc", "md", "txt", "pdf"}:
        surfaces.update({"sections", "required_facts", "tables"})
    if _text_has(text, "at least", "all ", "each ", "every ", "complete", "correctly include"):
        surfaces.add("coverage")
    if _text_has(text, "chart", "visual", "image", "figure"):
        surfaces.add("visual_elements")
    return sorted(surfaces)


def _deliverable_family(primary_group: str, outputs: list[str], task: str, rubrics: list[str]) -> str:
    text = f"{task} {' '.join(rubrics)}".lower()
    joined_outputs = " ".join(outputs).lower()
    file_operation = (
        re.search(r"\b(rename|archive|copy|move)\b", text) is not None
        or "delete the rest" in text
        or "target directory" in text
        or "output directory" in text
    )

    if file_operation or primary_group == "file_archive":
        return "file_archive_rename"
    if _text_has(text, "bug", "debug", "code review", "vulnerability"):
        return "bug_report"
    if primary_group == "presentation":
        return "business_presentation"
    if primary_group in {"spreadsheet", "csv_table"}:
        if _text_has(text, "matrix", "permission matrix", "mapping table", "cross-tab", "crosstab"):
            return "matrix_table"
        if _text_has(text, "cleaned", "clean ", "normalize", "deduplicate"):
            return "cleaned_table"
        if _text_has(text, "schedule", "timeline", "plan", "roadmap", "calendar"):
            return "plan_timeline_table"
        return "summary_workbook" if primary_group == "spreadsheet" else "csv_table"
    if primary_group in {"document_report", "plain_markdown"}:
        if _text_has(text, "sop", "procedure", "manual", "operations manual", "commitment letter"):
            return "sop_process_document"
        if _text_has(text, "plan", "timeline", "schedule", "action plan") and not _text_has(joined_outputs, "report"):
            return "project_plan_document"
        return "analytical_report"
    if primary_group == "pdf_or_visual_document":
        if _text_has(text, "resume", "cover letter"):
            return "resume_or_pdf_document"
        return "visual_document"
    if primary_group == "code_or_structured_artifact":
        return "code_or_structured_artifact"
    return f"{primary_group}_generation"


def _cluster_id(family: str, primary_group: str) -> str:
    mapping = {
        "analytical_report": "analytical_report_generation",
        "summary_workbook": "spreadsheet_summary_workbook_generation",
        "matrix_table": "spreadsheet_matrix_generation",
        "cleaned_table": "csv_cleaning_and_table_generation",
        "csv_table": "csv_cleaning_and_table_generation",
        "business_presentation": "business_presentation_generation",
        "sop_process_document": "sop_process_document_generation",
        "project_plan_document": "project_plan_timeline_generation",
        "plan_timeline_table": "project_plan_timeline_generation",
        "file_archive_rename": "file_archive_rename_generation",
        "bug_report": "bug_report_generation",
        "resume_or_pdf_document": "pdf_resume_or_visual_document_generation",
        "visual_document": "pdf_resume_or_visual_document_generation",
    }
    if family in mapping:
        return mapping[family]
    if primary_group == "code_or_structured_artifact":
        return "code_or_structured_artifact_generation"
    return _safe_slug(family)


def _cluster_description(cluster_id: str) -> str:
    descriptions = {
        "analytical_report_generation": "reports and narrative documents that must turn collected facts into structured analysis, calculations, tables, conclusions, and recommendations",
        "spreadsheet_summary_workbook_generation": "Excel workbooks with multiple sheets, summaries, statistics, exceptions, and exact read-back checks",
        "spreadsheet_matrix_generation": "matrix-style spreadsheets such as permission, responsibility, mapping, or cross-tab tables",
        "business_presentation_generation": "PowerPoint decks that convert collected facts into a slide structure with metrics, comparisons, visuals, and conclusions",
        "sop_process_document_generation": "manuals, SOPs, process documents, commitments, and operational guidance documents",
        "project_plan_timeline_generation": "plans, schedules, timelines, roadmaps, action tables, and execution tracking artifacts",
        "csv_cleaning_and_table_generation": "CSV or flat-table outputs that require cleaning, normalization, stable columns, and row-level validation",
        "file_archive_rename_generation": "tasks whose final output requires exact file selection, copying, moving, renaming, deletion, archives, or directory layouts",
        "bug_report_generation": "bug reports, debugging writeups, code issue summaries, and fix recommendation documents",
        "pdf_resume_or_visual_document_generation": "PDF, resume, image, and visual deliverables with strict layout or visual-element requirements",
        "code_or_structured_artifact_generation": "generated scripts, JSON files, and other structured machine-readable artifacts",
    }
    return descriptions.get(cluster_id, "specialized final deliverable generation tasks")


def prepare_data(source_eval_root: Path, eval_root: Path) -> None:
    source_tasks = source_eval_root / "tasks_full_minus_lite"
    source_manifest = source_eval_root / "tasks_full_minus_lite_manifest.json"
    dest_tasks = eval_root / "tasks_full_minus_lite"
    dest_manifest = eval_root / "tasks_full_minus_lite_manifest.json"

    if not source_tasks.is_dir():
        raise SystemExit(f"source tasks dir not found: {source_tasks}")
    if not source_manifest.is_file():
        raise SystemExit(f"source manifest not found: {source_manifest}")

    if dest_tasks.exists():
        shutil.rmtree(dest_tasks)
    shutil.copytree(source_tasks, dest_tasks)
    shutil.copy2(source_manifest, dest_manifest)
    print(f"[prepare-data] copied {source_tasks} -> {dest_tasks}")
    print(f"[prepare-data] copied {source_manifest} -> {dest_manifest}")


def build_profiles(eval_root: Path, output_dir: Path) -> list[dict[str, Json]]:
    tasks_root = eval_root / "tasks_full_minus_lite"
    if not tasks_root.is_dir():
        raise SystemExit(f"tasks_full_minus_lite not found under {eval_root}; run prepare-data first")

    profiles: list[dict[str, Json]] = []
    for meta_path in sorted(tasks_root.glob("*/metadata.json"), key=_task_sort_key):
        meta = _read_json(meta_path)
        outputs = [str(v) for v in meta.get("output_files") or []]
        rubrics = [str(v) for v in meta.get("rubrics") or []]
        output_exts = sorted({_output_ext(output) for output in outputs})
        primary_ext = output_exts[0] if len(output_exts) == 1 else (_output_ext(outputs[0]) if outputs else "none")
        primary_group = _format_group(primary_ext)
        source_exts = _extract_source_exts(meta)
        task_text = str(meta.get("task") or "")
        rubric_text = " ".join(rubrics)
        family = _deliverable_family(primary_group, outputs, task_text, rubrics)

        profile = {
            "id": str(meta.get("id") or meta_path.parent.name),
            "absolute_id": meta.get("absolute_id"),
            "persona": meta.get("persona"),
            "job": meta.get("job"),
            "task_diff": meta.get("task_diff"),
            "task": task_text,
            "output_files": outputs,
            "output_exts": output_exts,
            "primary_output_format": primary_group,
            "deliverable_family": family,
            "cluster_id": _cluster_id(family, primary_group),
            "source_exts": source_exts,
            "dependency_edge_count": len(meta.get("file_dep_graph") or []),
            "operation_tags": _operation_tags(task_text, rubric_text),
            "validation_surface": _validation_surface(outputs, rubrics),
            "rubric_count": len(rubrics),
            "rubric_types": meta.get("rubric_types") or [],
            "tested_capabilities": meta.get("tested_capabilities") or [],
            "rubrics": rubrics,
            "data_manifest": meta.get("data_manifest") or [],
            "metadata_path": str(meta_path),
        }
        profiles.append(profile)

    output_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = output_dir / "task_profiles.jsonl"
    profiles_path.write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in profiles),
        encoding="utf-8",
    )
    print(f"[profile] wrote {len(profiles)} profiles to {profiles_path}")
    return profiles


def _load_profiles(output_dir: Path) -> list[dict[str, Json]]:
    profiles_path = output_dir / "task_profiles.jsonl"
    if not profiles_path.is_file():
        raise SystemExit(f"profile file not found: {profiles_path}; run profile first")
    return [json.loads(line) for line in profiles_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sample_tasks(items: list[dict[str, Json]], limit: int = 8) -> list[dict[str, Json]]:
    ranked = sorted(
        items,
        key=lambda p: (
            -int(p.get("rubric_count") or 0),
            -int(p.get("dependency_edge_count") or 0),
            int(p.get("id") or 0) if str(p.get("id") or "").isdigit() else 999999,
        ),
    )
    return ranked[:limit]


def build_clusters(output_dir: Path, max_clusters: int) -> dict[str, Json]:
    profiles = _load_profiles(output_dir)
    buckets: dict[str, list[dict[str, Json]]] = defaultdict(list)
    for profile in profiles:
        buckets[str(profile["cluster_id"])].append(profile)

    sorted_buckets = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if len(sorted_buckets) > max_clusters:
        keep = dict(sorted_buckets[: max_clusters - 1])
        merged: list[dict[str, Json]] = []
        for _, items in sorted_buckets[max_clusters - 1 :]:
            merged.extend(items)
        keep["misc_generation"] = merged
        sorted_buckets = sorted(keep.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    clusters: dict[str, Json] = {}
    for cluster_id, items in sorted_buckets:
        output_exts = Counter(ext for p in items for ext in p.get("output_exts", []))
        source_exts = Counter(ext for p in items for ext in p.get("source_exts", []))
        operation_tags = Counter(tag for p in items for tag in p.get("operation_tags", []))
        validation_surface = Counter(tag for p in items for tag in p.get("validation_surface", []))
        personas = Counter(str(p.get("persona")) for p in items)
        difficulties = Counter(str(p.get("task_diff")) for p in items)
        representatives = _sample_tasks(items)
        clusters[cluster_id] = {
            "cluster_id": cluster_id,
            "description": _cluster_description(cluster_id),
            "task_count": len(items),
            "output_exts": dict(output_exts.most_common()),
            "source_exts": dict(source_exts.most_common()),
            "operation_tags": dict(operation_tags.most_common()),
            "validation_surface": dict(validation_surface.most_common()),
            "personas": dict(personas.most_common()),
            "difficulties": dict(difficulties.most_common()),
            "task_ids": [p["id"] for p in items],
            "representative_tasks": [
                {
                    "id": p["id"],
                    "task": p["task"],
                    "output_files": p["output_files"],
                    "rubrics": p["rubrics"][:8],
                    "operation_tags": p["operation_tags"],
                    "validation_surface": p["validation_surface"],
                }
                for p in representatives
            ],
        }

    _write_json(output_dir / "clusters.json", {"clusters": clusters, "profile_count": len(profiles)})
    _write_cluster_assignments(output_dir / "cluster_assignments.csv", profiles, clusters)
    _write_cluster_report(output_dir / "cluster_report.md", clusters, generated=False)
    print(f"[cluster] wrote {len(clusters)} clusters to {output_dir / 'clusters.json'}")
    _print_cluster_table(clusters)
    return clusters


def _write_cluster_assignments(path: Path, profiles: list[dict[str, Json]], clusters: dict[str, Json]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "task_id",
                "cluster_id",
                "cluster_description",
                "output_files",
                "primary_output_format",
                "deliverable_family",
                "operation_tags",
                "validation_surface",
                "task",
            ],
        )
        writer.writeheader()
        for profile in sorted(profiles, key=lambda p: int(p["id"]) if str(p["id"]).isdigit() else 999999):
            cluster_id = str(profile["cluster_id"])
            writer.writerow(
                {
                    "task_id": profile["id"],
                    "cluster_id": cluster_id,
                    "cluster_description": clusters.get(cluster_id, {}).get("description", ""),
                    "output_files": "; ".join(profile.get("output_files") or []),
                    "primary_output_format": profile.get("primary_output_format"),
                    "deliverable_family": profile.get("deliverable_family"),
                    "operation_tags": "; ".join(profile.get("operation_tags") or []),
                    "validation_surface": "; ".join(profile.get("validation_surface") or []),
                    "task": profile.get("task"),
                }
            )


def _print_cluster_table(clusters: dict[str, Json]) -> None:
    print("[cluster] overview:")
    for cluster_id, cluster in sorted(clusters.items(), key=lambda kv: (-kv[1]["task_count"], kv[0])):
        print(f"  - {cluster_id}: {cluster['task_count']} tasks")


def _load_clusters(output_dir: Path) -> dict[str, Json]:
    path = output_dir / "clusters.json"
    if not path.is_file():
        raise SystemExit(f"clusters file not found: {path}; run cluster first")
    data = _read_json(path)
    return data["clusters"]


def _cluster_title(cluster_id: str) -> str:
    return cluster_id.replace("_", " ").title()


def _write_representatives(path: Path, cluster: dict[str, Json]) -> None:
    lines = [f"# {_cluster_title(cluster['cluster_id'])}", "", f"Task count: {cluster['task_count']}", ""]
    for task in cluster["representative_tasks"]:
        lines.extend(
            [
                f"## Task {task['id']}",
                "",
                task["task"],
                "",
                f"Outputs: {', '.join(task['output_files'])}",
                "",
                "Rubric sample:",
            ]
        )
        for rubric in task["rubrics"]:
            lines.append(f"- {rubric}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _build_skill_prompt(cluster: dict[str, Json]) -> str:
    compact = {
        "cluster_id": cluster["cluster_id"],
        "task_count": cluster["task_count"],
        "output_exts": cluster["output_exts"],
        "source_exts": cluster["source_exts"],
        "operation_tags": cluster["operation_tags"],
        "validation_surface": cluster["validation_surface"],
        "representative_tasks": cluster["representative_tasks"],
    }
    return textwrap.dedent(
        f"""
        You are creating a YiYi SKILL.md for Workspace-Bench final deliverable generation.

        Scope:
        - The skill is only for final artifact generation.
        - Do not mention internal pipeline stages, stage names, or phase numbers.
        - Do not teach file discovery or broad source collection; assume the agent already has the relevant source facts, tables, and coverage notes.
        - Teach the agent how to construct the final deliverable, perform required computations/transformations, and read back validate the artifact.
        - Use English.
        - Keep the skill practical and directive.

        Output one complete SKILL.md with YAML frontmatter:
        ---
        name: {cluster['cluster_id']}
        description: "..."
        metadata:
          {{"yiyi": {{"emoji": "...", "requires": {{}}}}}}
        ---

        Required sections:
        # <Title>
        ## When to use this skill
        ## Inputs expected from Phase 2
        ## Generation workflow
        ## Required computations and transformations
        ## Format-specific construction rules
        ## Read-back validation checklist
        ## Common failure modes

        Cluster evidence:
        ```json
        {json.dumps(compact, ensure_ascii=False, indent=2)}
        ```
        """
    ).strip() + "\n"


def _fallback_skill(cluster: dict[str, Json]) -> str:
    name = cluster["cluster_id"]
    title = _cluster_title(name)
    description = cluster.get("description") or "final artifact generation"
    operations = ", ".join(cluster.get("operation_tags", {}).keys()) or "artifact generation"
    validation = ", ".join(cluster.get("validation_surface", {}).keys()) or "file readability"
    return textwrap.dedent(
        f"""\
        ---
        name: {name}
        description: "Use this skill for Workspace-Bench tasks involving {description}; it guides final artifact construction, required computations, formatting, and read-back validation."
        metadata:
          {{"yiyi": {{"emoji": "🧩", "requires": {{}}}}}}
        ---

        # {title}

        ## When to use this skill
        Use this skill when the relevant facts, tables, source ledger, and coverage notes are already available and the remaining work is to create the final deliverable.

        ## Inputs expected before writing
        - Task instruction and exact expected output filename.
        - Complete source facts and tables needed for the final artifact.
        - Required entities, dates, metrics, sections, and coverage notes.
        - Any known missing or risky fields that must be resolved before writing.

        ## Generation workflow
        1. Restate the required output file, format, and visible structure before writing.
        2. Design the artifact schema: sections, sheets, slides, columns, filenames, or directory layout.
        3. Map every required section or field to collected facts before producing prose or tables.
        4. Perform the required transformations: {operations}.
        5. Write the final artifact to the exact requested path and filename.
        6. Read the artifact back and validate it before finishing.

        ## Required computations and transformations
        - Compute totals, percentages, averages, medians, rankings, and Top/Bottom lists when the task or rubrics imply quantitative analysis.
        - Preserve exact numbers, units, dates, entity names, and category labels from Phase 2.
        - Prefer explicit tables for dense metrics and concise prose for conclusions.
        - If visual elements are required, generate charts from the underlying data rather than decorative images.

        ## Format-specific construction rules
        - Match the requested extension exactly.
        - Use stable headings, sheet names, slide titles, column names, and file names.
        - Keep generated content auditable: each conclusion should trace back to provided facts.

        ## Read-back validation checklist
        Check these surfaces before final response: {validation}.

        ## Common failure modes
        - Writing the right content to the wrong filename or directory.
        - Producing plausible prose without required numeric calculations.
        - Omitting required tables, sections, sheets, slides, or records.
        - Not reading the final artifact back after writing it.
        """
    )


def _load_model_config(judge_yaml: Path | None) -> dict[str, str]:
    if judge_yaml and judge_yaml.is_file():
        cfg = _read_simple_yaml(judge_yaml)
        base_url = cfg.get("baseUrl") or cfg.get("base_url") or ""
        api_key = cfg.get("apiKey") or cfg.get("api_key") or ""
        model = cfg.get("model") or cfg.get("model_name") or ""
        model_name = cfg.get("model_name") or model
        if base_url and api_key and model:
            return {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "model_name": model_name,
                "source": str(judge_yaml),
            }

    base_url = os.environ.get("SKILL_LEARNING_BASE_URL", "")
    api_key = os.environ.get("SKILL_LEARNING_API_KEY", "")
    model = os.environ.get("SKILL_LEARNING_MODEL", "")
    if base_url and api_key and model:
        return {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "model_name": model,
            "source": "SKILL_LEARNING_* env",
        }
    return {}


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _call_openai_compatible(prompt: str, model_cfg: dict[str, str]) -> str | None:
    base_url = model_cfg.get("base_url", "")
    api_key = model_cfg.get("api_key", "")
    model = model_cfg.get("model", "")
    if not base_url or not api_key or not model:
        return None

    url = _chat_completions_url(base_url)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You write concise, executable SKILL.md files for AI agents."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[generate-skills] LLM request failed: {exc}", file=sys.stderr)
        return None

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return str(content).strip() if content else None


def _clean_skill_content(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:markdown|md)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    content = re.sub(r"\bPhase\s*3\b", "final artifact creation", content, flags=re.IGNORECASE)
    content = re.sub(r"\bPhase\s*2\b", "collected source material", content, flags=re.IGNORECASE)
    content = re.sub(r"\bPhase\s+Three\b", "final artifact creation", content, flags=re.IGNORECASE)
    content = re.sub(r"\bphase\s+three\b", "final artifact creation", content, flags=re.IGNORECASE)
    content = content.replace("Inputs expected from final generation step", "Inputs expected before writing")
    content = content.replace("from earlier processing stages", "from prior source collection")
    return content.strip()


def generate_skills(output_dir: Path, judge_yaml: Path | None, only_clusters: set[str] | None = None) -> None:
    all_clusters = _load_clusters(output_dir)
    clusters = all_clusters
    if only_clusters:
        missing = sorted(only_clusters - set(all_clusters))
        if missing:
            raise SystemExit(f"unknown cluster id(s): {', '.join(missing)}")
        clusters = {k: v for k, v in all_clusters.items() if k in only_clusters}
    model_cfg = _load_model_config(judge_yaml)
    prompts_dir = output_dir / "prompts"
    skills_dir = output_dir / "skill_candidates"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    status_path = output_dir / "generation_status.json"
    if status_path.is_file():
        try:
            statuses: dict[str, str] = _read_json(status_path)
        except Exception:
            statuses = {}
    else:
        statuses = {}
    for cluster_id, cluster in clusters.items():
        prompt = _build_skill_prompt(cluster)
        prompt_path = prompts_dir / f"{cluster_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        cluster_dir = skills_dir / cluster_id
        cluster_dir.mkdir(parents=True, exist_ok=True)
        _write_json(cluster_dir / "cluster_summary.json", cluster)
        _write_representatives(cluster_dir / "representative_tasks.md", cluster)

        content = _call_openai_compatible(prompt, model_cfg)
        if content is None:
            content = _fallback_skill(cluster)
            content = _clean_skill_content(content)
            statuses[cluster_id] = "fallback_template"
        else:
            content = _clean_skill_content(content)
            statuses[cluster_id] = "llm_generated"
        (cluster_dir / "SKILL.md").write_text(content.rstrip() + "\n", encoding="utf-8")
        _write_json(output_dir / "generation_status.json", statuses)

    _write_json(output_dir / "generation_status.json", statuses)
    safe_model_cfg = {
        "source": model_cfg.get("source"),
        "base_url": model_cfg.get("base_url"),
        "model": model_cfg.get("model"),
        "model_name": model_cfg.get("model_name"),
        "has_api_key": bool(model_cfg.get("api_key")),
    }
    _write_json(output_dir / "generation_model.json", safe_model_cfg)
    _write_cluster_report(output_dir / "cluster_report.md", all_clusters, generated=True, statuses=statuses)
    print(f"[generate-skills] wrote {len(clusters)} skill candidates to {skills_dir}")


def _write_cluster_report(path: Path, clusters: dict[str, Json], *, generated: bool, statuses: dict[str, str] | None = None) -> None:
    lines = ["# Generation Skill Learning Cluster Report", ""]
    lines.append(f"Clusters: {len(clusters)}")
    lines.append("")
    for cluster_id, cluster in sorted(clusters.items(), key=lambda kv: (-kv[1]["task_count"], kv[0])):
        status = f" ({statuses.get(cluster_id)})" if statuses else ""
        lines.extend(
            [
                f"## {cluster_id}{status}",
                "",
                f"- Tasks: {cluster['task_count']}",
                f"- Description: {cluster.get('description', '')}",
                f"- Output extensions: {cluster['output_exts']}",
                f"- Source extensions: {cluster['source_exts']}",
                f"- Operations: {cluster['operation_tags']}",
                f"- Validation: {cluster['validation_surface']}",
                f"- Representative IDs: {', '.join(t['id'] for t in cluster['representative_tasks'][:8])}",
                "",
            ]
        )
    if not generated:
        lines.append("Run `generate-skills` to create prompts and SKILL.md drafts.")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Learn Phase-3 generation skills from Workspace-Bench task metadata.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--eval-root", default=".", help="Workspace-Bench/evaluation root")
        p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory relative to eval-root unless absolute")

    p = sub.add_parser("prepare-data", help="Copy tasks_full_minus_lite into eval-root")
    p.add_argument("--source-eval-root", default=str(DEFAULT_SOURCE_EVAL_ROOT))
    p.add_argument("--eval-root", default=".")

    for name in ("profile", "cluster", "generate-skills", "all"):
        p = sub.add_parser(name)
        add_common(p)
        if name == "cluster":
            p.add_argument("--max-clusters", type=int, default=12)
        if name == "all":
            p.add_argument("--source-eval-root", default=str(DEFAULT_SOURCE_EVAL_ROOT))
            p.add_argument("--max-clusters", type=int, default=12)
        if name in {"generate-skills", "all"}:
            p.add_argument("--judge-yaml", default=str(DEFAULT_JUDGE_YAML), help="Judge model YAML with baseUrl/model/apiKey")
            p.add_argument("--clusters", help="Comma-separated cluster ids to generate")

    return parser.parse_args()


def _resolve_output_dir(eval_root: Path, output_dir: str) -> Path:
    path = Path(output_dir)
    return path if path.is_absolute() else eval_root / path


def main() -> None:
    args = parse_args()
    command = args.command

    if command == "prepare-data":
        prepare_data(Path(args.source_eval_root).resolve(), Path(args.eval_root).resolve())
        return

    eval_root = Path(args.eval_root).resolve()
    output_dir = _resolve_output_dir(eval_root, args.output_dir)

    if command == "profile":
        build_profiles(eval_root, output_dir)
    elif command == "cluster":
        build_clusters(output_dir, args.max_clusters)
    elif command == "generate-skills":
        judge_yaml = Path(args.judge_yaml)
        if not judge_yaml.is_absolute():
            judge_yaml = eval_root / judge_yaml
        only_clusters = {x.strip() for x in args.clusters.split(",") if x.strip()} if args.clusters else None
        generate_skills(output_dir, judge_yaml, only_clusters)
    elif command == "all":
        prepare_data(Path(args.source_eval_root).resolve(), eval_root)
        build_profiles(eval_root, output_dir)
        build_clusters(output_dir, args.max_clusters)
        judge_yaml = Path(args.judge_yaml)
        if not judge_yaml.is_absolute():
            judge_yaml = eval_root / judge_yaml
        only_clusters = {x.strip() for x in args.clusters.split(",") if x.strip()} if args.clusters else None
        generate_skills(output_dir, judge_yaml, only_clusters)


if __name__ == "__main__":
    main()
