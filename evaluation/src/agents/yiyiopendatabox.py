import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

Json = Any


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(path: str, *, limit: int = 500_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit + 1)[:limit]
    except Exception:
        return ""


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_project_root() -> str:
    # .../OpenDataBox/Workspace-Bench/evaluation/src/agents/yiyiopendatabox.py
    return str(Path(__file__).resolve().parents[4])


def _configured_str(value: Json) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = os.path.expandvars(value.strip()).strip()
    if not s or re.search(r"\$\{?[^}\s]+\}?", s):
        return None
    return s


def _project_root(api_provider: Dict[str, Json]) -> str:
    configured = (
        _configured_str(api_provider.get("projectRoot"))
        or _configured_str(api_provider.get("project_root"))
        or _configured_str(os.environ.get("YIYI_OPENDATABOX_PROJECT_ROOT"))
        or _configured_str(os.environ.get("OPENDATABOX_PROJECT_ROOT"))
    )
    return os.path.abspath(str(configured)) if configured else _default_project_root()


def _cargo_root(project_root: str, api_provider: Dict[str, Json]) -> str:
    configured = (
        _configured_str(api_provider.get("cargoRoot"))
        or _configured_str(api_provider.get("cargo_root"))
        or _configured_str(os.environ.get("YIYI_CARGO_ROOT"))
    )
    if configured:
        return os.path.abspath(str(configured))
    return os.path.join(project_root, "YiYi", "app", "src-tauri")


def _eval_cmd(cargo_root: str, api_provider: Dict[str, Json]) -> List[str]:
    configured = (
        _configured_str(api_provider.get("evalBin"))
        or _configured_str(api_provider.get("eval_bin"))
        or _configured_str(os.environ.get("YIYI_EVAL_BIN"))
    )
    candidates = []
    if configured:
        candidates.append(os.path.abspath(str(configured)))
    candidates.append(os.path.join(cargo_root, "target", "debug", "eval"))
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return [p]
    return ["cargo", "run", "--bin", "eval", "--no-default-features", "--"]


def _metadata_task_dir(sandbox_dir: str) -> str:
    meta_path = os.path.join(sandbox_dir, "metadata.json")
    try:
        meta = _read_json(meta_path)
    except Exception:
        return sandbox_dir
    source = meta.get("__metadata_path") if isinstance(meta, dict) else None
    if isinstance(source, str) and source.strip() and os.path.isfile(source):
        return os.path.dirname(os.path.abspath(source))
    return sandbox_dir


def _prepare_yiyi_working_dir(task_id: str, api_provider: Dict[str, Json]) -> str:
    home_yiyi = (
        _configured_str(api_provider.get("homeYiyi"))
        or _configured_str(api_provider.get("home_yiyi"))
        or _configured_str(os.environ.get("YIYI_HOME"))
        or os.path.join(os.path.expanduser("~"), ".yiyi")
    )
    src_root = os.path.abspath(str(home_yiyi))
    tmp_dir = tempfile.mkdtemp(prefix=f"yiyi_wb_{task_id}_")

    for name in ("config.json", "yiyi.db", ".env"):
        src = os.path.join(src_root, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(tmp_dir, name))

    models_src = os.path.join(src_root, "models")
    if os.path.isdir(models_src):
        try:
            os.symlink(models_src, os.path.join(tmp_dir, "models"))
        except FileExistsError:
            pass
        except OSError:
            pass
    return tmp_dir


def _normalize_rel(p: str) -> str:
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    return s


def _resolve_under(root: str, p: str) -> Optional[str]:
    rel = _normalize_rel(p)
    if not rel:
        return None
    ap = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if ap == root_abs or ap.startswith(root_abs + os.sep):
        return ap
    return None


def _collect_outputs(output_dir: str, expected_files: List[str]) -> List[str]:
    if not os.path.isdir(output_dir):
        return []

    skipped = {"trace.txt", "trace.json"}
    skipped_suffixes = (".bak", ".tmp", ".log")
    expected = [os.path.basename(str(x)) for x in expected_files if isinstance(x, str) and str(x).strip()]
    found: List[str] = []

    if expected:
        want = set(expected)
        for root, _, files in os.walk(output_dir):
            for name in files:
                if name in skipped:
                    continue
                if name in want:
                    found.append(os.path.abspath(os.path.join(root, name)))
        if found:
            return sorted(set(found))

    for root, _, files in os.walk(output_dir):
        for name in files:
            if name in skipped:
                continue
            if name.endswith(skipped_suffixes):
                continue
            found.append(os.path.abspath(os.path.join(root, name)))
    return sorted(set(found))


def _move_trace_files(output_dir: str, raw_dir: str) -> None:
    for name in ("trace.txt", "trace.json"):
        src = os.path.join(output_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(raw_dir, f"yiyi_{name}")
        try:
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
        except Exception:
            pass


def _trace_entries_from_yiyi(raw_dir: str, *, prompt: str, started_at: float, model: Optional[str]) -> Dict[str, Json]:
    trace_json_path = os.path.join(raw_dir, "yiyi_trace.json")
    execution_trace: List[Dict[str, Json]] = [
        {"type": "text", "role": "user", "content": str(prompt or ""), "timestamp": _iso_from_ts(started_at)}
    ]
    last_text = ""
    turns = 0
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}

    try:
        trace_json = _read_json(trace_json_path)
    except Exception:
        trace_json = {}

    if isinstance(trace_json, dict):
        if isinstance(trace_json.get("result"), str):
            last_text = str(trace_json.get("result") or "")
        entries = trace_json.get("entries")
        if isinstance(entries, list):
            ts_base = started_at
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                ts = _iso_from_ts(ts_base + ((idx + 1) / 1000.0))
                typ = entry.get("type")
                if typ in {"thinking", "token"}:
                    content = entry.get("content") if isinstance(entry.get("content"), str) else ""
                    if not content:
                        continue
                    turns += 1
                    if typ == "token":
                        last_text = content
                    execution_trace.append(
                        {
                            "type": "text",
                            "role": "assistant",
                            "content": content,
                            "timestamp": ts,
                            "turn": turns,
                            "llm": {
                                "provider": "yiyi-opendatabox",
                                "baseUrl": None,
                                "model": model,
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0},
                                "stopReason": None,
                                "errorMessage": None,
                            },
                        }
                    )
                elif typ == "tool_start":
                    execution_trace.append(
                        {
                            "type": "tool",
                            "role": "tool",
                            "tool": entry.get("name"),
                            "callID": f"yiyi_tool_{idx}",
                            "timestamp": ts,
                            "startedAt": ts,
                            "finishedAt": None,
                            "durationMs": None,
                            "status": "in_progress",
                            "exitCode": None,
                            "input": {"args_preview": entry.get("args_preview")},
                            "output": None,
                            "turn": turns or None,
                        }
                    )
                elif typ == "tool_end":
                    execution_trace.append(
                        {
                            "type": "tool",
                            "role": "tool",
                            "tool": entry.get("name"),
                            "callID": f"yiyi_tool_{idx}",
                            "timestamp": ts,
                            "startedAt": ts,
                            "finishedAt": ts,
                            "durationMs": None,
                            "status": "completed",
                            "exitCode": None,
                            "input": {},
                            "output": entry.get("result_preview"),
                            "turn": turns or None,
                        }
                    )
                elif typ == "usage":
                    pt = int(entry.get("input_tokens") or 0)
                    ct = int(entry.get("output_tokens") or 0)
                    cr = int(entry.get("cache_read_tokens") or 0)
                    usage_total["prompt_tokens"] += pt
                    usage_total["completion_tokens"] += ct
                    usage_total["total_tokens"] += pt + ct
                    usage_total["cache_read"] += cr

    if not last_text:
        last_text = _read_text(os.path.join(raw_dir, "yiyi_trace.txt"), limit=100_000).strip()
    return {
        "executionTrace": execution_trace,
        "lastText": last_text,
        "turns": turns or None,
        "usageTotal": usage_total,
    }


def _model_label(api_provider: Dict[str, Json]) -> Optional[str]:
    for key in ("model", "model_name", "modelName"):
        value = api_provider.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: Dict[str, Json],
    agent_id: Optional[str] = None,
) -> Dict[str, Json]:
    started_at = time.time()
    api_provider = dict(api_provider) if isinstance(api_provider, dict) else {}
    _ensure_dir(sandbox_dir)
    raw_dir = os.path.join(sandbox_dir, "raw")
    _ensure_dir(raw_dir)

    task_id = os.path.basename(os.path.abspath(sandbox_dir)) or "case"
    project_root = _project_root(api_provider)
    cargo_root = _cargo_root(project_root, api_provider)
    task_dir = _metadata_task_dir(sandbox_dir)
    output_dir = os.path.join(os.path.abspath(work_dir), "model_output")
    _ensure_dir(output_dir)

    cmd = _eval_cmd(cargo_root, api_provider) + [
        "--task-dir",
        os.path.abspath(task_dir),
        "--fs-root",
        os.path.abspath(work_dir),
        "--output-dir",
        os.path.abspath(output_dir),
    ]
    yiyi_working_dir = _prepare_yiyi_working_dir(task_id, api_provider)

    _write_json(
        os.path.join(raw_dir, "yiyi_invocation.json"),
        {
            "cmd": cmd,
            "cwd": os.path.abspath(cargo_root),
            "projectRoot": project_root,
            "cargoRoot": cargo_root,
            "taskDir": os.path.abspath(task_dir),
            "fsRoot": os.path.abspath(work_dir),
            "outputDir": os.path.abspath(output_dir),
            "yiyiWorkingDir": yiyi_working_dir,
            "agentId": agent_id,
        },
    )

    env = os.environ.copy()
    env["YIYI_WORKING_DIR"] = yiyi_working_dir
    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    stdout_text = ""
    stderr_text = ""
    exit_code = 1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(cargo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(timeout=used_timeout)
            exit_code = int(proc.returncode or 0)
        except subprocess.TimeoutExpired:
            exit_code = 124
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                stdout_text, stderr_text = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout_text, stderr_text = proc.communicate()
    except Exception as e:
        stderr_text = str(e)
        exit_code = 1
    finally:
        _move_trace_files(output_dir, raw_dir)
        if str(os.environ.get("YIYI_KEEP_EVAL_TMP") or "").strip().lower() not in {"1", "true", "yes", "on"}:
            shutil.rmtree(yiyi_working_dir, ignore_errors=True)

    for name, text in {
        "stdout.txt": stdout_text or "",
        "stderr.txt": stderr_text or "",
        "runner_stdout.txt": stdout_text or "",
        "runner_stderr.txt": stderr_text or "",
    }.items():
        with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    expected = api_provider.get("__expected_output_files__")
    expected_files = expected if isinstance(expected, list) else []
    paths = _collect_outputs(output_dir, expected_files)
    trace_core = _trace_entries_from_yiyi(raw_dir, prompt=prompt, started_at=started_at, model=_model_label(api_provider))

    if exit_code == 124:
        status = "timeout"
        error_message = f"Timeout after {timeout_s}s"
    elif exit_code != 0:
        status = "error"
        error_message = (stderr_text or stdout_text or "YiYi eval runner failed")[:4000]
    else:
        status = "ok"
        error_message = None

    usage_total = trace_core.get("usageTotal") if isinstance(trace_core.get("usageTotal"), dict) else {}
    metrics = {
        "turns": trace_core.get("turns") if isinstance(trace_core.get("turns"), int) else None,
        "promptTokens": int(usage_total.get("prompt_tokens") or 0),
        "completionTokens": int(usage_total.get("completion_tokens") or 0),
        "totalTokens": int(usage_total.get("total_tokens") or 0),
    }

    return {
        "status": status,
        "paths": paths,
        "errorMessage": error_message,
        "trace": {
            "runner": "yiyi-opendatabox",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": str(trace_core.get("lastText") or ""),
            "executionTrace": trace_core.get("executionTrace") if isinstance(trace_core.get("executionTrace"), list) else [],
            "llm": {"provider": "yiyi-opendatabox", "baseUrl": None, "model": _model_label(api_provider)},
            "usageTotal": usage_total,
            "outputDir": os.path.abspath(output_dir),
        },
        "metrics": metrics,
        "durationMs": int((time.time() - started_at) * 1000),
    }
