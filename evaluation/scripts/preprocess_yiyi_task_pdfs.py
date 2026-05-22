#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

Json = Any


def _read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _configured_str(value: Json) -> str | None:
    if not isinstance(value, str):
        return None
    s = os.path.expandvars(value.strip()).strip()
    return s or None


def _task_sort_key(path: Path) -> tuple[int, str]:
    try:
        return (0, f"{int(path.parent.name):09d}")
    except Exception:
        return (1, path.parent.name)


def _normalize_rel_path(path: str) -> Path:
    s = path.strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    return Path(s)


def _manifest_target_rels(item: dict[str, Json]) -> list[Path]:
    target_path = item.get("target_path")
    stored_relpath = item.get("stored_relpath")
    filename = item.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        filename = os.path.basename(str(stored_relpath or "").strip())
    if not isinstance(target_path, str) or not target_path.strip():
        return []

    rel = _normalize_rel_path(target_path)
    out = [rel]
    if filename and not (str(rel).endswith("/" + filename.strip()) or str(rel) == filename.strip()):
        out.append(rel / filename.strip())
    return out


def _collect_task_pdf_refs(tasks_root: Path) -> tuple[dict[str, set[str]], dict[str, set[Path]]]:
    names_by_role: dict[str, set[str]] = {}
    rels_by_role: dict[str, set[Path]] = {}
    for meta_path in sorted(tasks_root.glob("*/metadata.json"), key=_task_sort_key):
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        role = meta.get("file_system")
        if not isinstance(role, str) or not role.strip():
            continue
        data_manifest = meta.get("data_manifest")
        if not isinstance(data_manifest, list):
            continue
        for item in data_manifest:
            if not isinstance(item, dict):
                continue
            stored_relpath = item.get("stored_relpath")
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename.strip():
                filename = os.path.basename(str(stored_relpath or "").strip())
            if not filename.lower().endswith(".pdf"):
                continue
            names_by_role.setdefault(role, set()).add(filename)
            for rel in _manifest_target_rels(item):
                if str(rel).lower().endswith(".pdf"):
                    rels_by_role.setdefault(role, set()).add(rel)
    return names_by_role, rels_by_role


def _index_role_dir(role_dir: Path, needed_names: set[str]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {name: [] for name in needed_names}
    if not role_dir.is_dir() or not needed_names:
        return index
    for root, dirs, files in os.walk(role_dir):
        dirs[:] = [d for d in dirs if d not in {".opendatabox_trees", "model_output"}]
        for file_name in files:
            if file_name in needed_names:
                index.setdefault(file_name, []).append(Path(root) / file_name)
    return index


def _find_pdf_paths(
    *,
    fs_map: dict[str, Json],
    names_by_role: dict[str, set[str]],
    rels_by_role: dict[str, set[Path]],
    include_workdirs: bool,
) -> list[Path]:
    dir_maps = []
    standard_map = fs_map.get("standard_work_dir")
    if isinstance(standard_map, dict):
        dir_maps.append(standard_map)
    if include_workdirs:
        work_map = fs_map.get("work_dir")
        if isinstance(work_map, dict):
            dir_maps.append(work_map)

    found: set[Path] = set()
    for role, needed_names in names_by_role.items():
        for dir_map in dir_maps:
            role_dir_value = dir_map.get(role)
            if not isinstance(role_dir_value, str) or not role_dir_value.strip():
                continue
            role_dir = Path(role_dir_value).resolve()
            for rel in rels_by_role.get(role, set()):
                candidate = (role_dir / rel).resolve()
                if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                    found.add(candidate)

            index = _index_role_dir(role_dir, needed_names)
            for paths in index.values():
                found.update(p.resolve() for p in paths if p.is_file())
    return sorted(found, key=lambda p: str(p))


def _preprocess_cmd(args: argparse.Namespace) -> list[str]:
    configured = (
        _configured_str(args.preprocess_bin)
        or _configured_str(os.environ.get("YIYI_PREPROCESS_BIN"))
    )
    if configured:
        return [str(Path(configured).resolve())]
    docker_target_dir = _configured_str(os.environ.get("YIYI_DOCKER_TARGET_DIR"))
    if docker_target_dir:
        candidate = Path(docker_target_dir).resolve() / "debug" / "preprocess"
        if candidate.is_file():
            return [str(candidate)]
    cargo_root = Path(args.cargo_root).resolve()
    candidate = cargo_root / "target" / "debug" / "preprocess"
    if candidate.is_file():
        return [str(candidate)]
    return ["cargo", "run", "--bin", "preprocess", "--no-default-features", "--"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Preprocess only PDFs referenced by Workspace-Bench task data manifests for YiYi.")
    parser.add_argument("--eval-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--tasks-root", help="Defaults to EVAL_ROOT/tasks_lite")
    parser.add_argument("--fs-map-file", required=True)
    parser.add_argument("--filesys-root", help="Defaults to the common parent of standard/work role dirs")
    default_cargo_root = os.environ.get(
        "YIYI_CARGO_ROOT",
        str(Path(__file__).resolve().parents[3] / "YiYi" / "app" / "src-tauri"),
    )
    parser.add_argument("--cargo-root", default=default_cargo_root)
    parser.add_argument("--preprocess-bin", default=os.environ.get("YIYI_PREPROCESS_BIN"))
    parser.add_argument(
        "--include-workdirs",
        action="store_true",
        help="Also write caches directly into per-agent work_dir entries. Defaults to standard_work_dir only.",
    )
    parser.add_argument("--no-workdirs", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    eval_root = Path(args.eval_root).resolve()
    tasks_root = Path(args.tasks_root).resolve() if args.tasks_root else eval_root / "tasks_lite"
    fs_map_path = Path(args.fs_map_file)
    if not fs_map_path.is_absolute():
        fs_map_path = eval_root / fs_map_path
    fs_map = _read_json(fs_map_path)
    if not isinstance(fs_map, dict):
        raise SystemExit(f"fs map is not an object: {fs_map_path}")

    names_by_role, rels_by_role = _collect_task_pdf_refs(tasks_root)
    pdf_paths = _find_pdf_paths(
        fs_map=fs_map,
        names_by_role=names_by_role,
        rels_by_role=rels_by_role,
        include_workdirs=args.include_workdirs and not args.no_workdirs,
    )

    if args.filesys_root:
        filesys_root = Path(args.filesys_root).resolve()
    else:
        role_dirs: list[str] = []
        for key in ("standard_work_dir", "work_dir"):
            value = fs_map.get(key)
            if isinstance(value, dict):
                role_dirs.extend(str(v) for v in value.values() if isinstance(v, str) and v.strip())
        filesys_root = Path(os.path.commonpath([str(Path(p).resolve()) for p in role_dirs])).resolve()

    out_dir = eval_root / ".generated" / "yiyi_pdf_preprocess"
    out_dir.mkdir(parents=True, exist_ok=True)
    file_list = out_dir / "pdf_file_list.txt"
    file_list.write_text("\n".join(str(p) for p in pdf_paths) + ("\n" if pdf_paths else ""), encoding="utf-8")

    manifest = {
        "tasksRoot": str(tasks_root),
        "fsMapFile": str(fs_map_path),
        "filesysRoot": str(filesys_root),
        "pdfCount": len(pdf_paths),
        "fileList": str(file_list),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not pdf_paths:
        print("[yiyi-pdf-preprocess] no task PDF paths found")
        return 0

    cmd = _preprocess_cmd(args) + ["--fs-root", str(filesys_root), "--file-list", str(file_list)]
    env = os.environ.copy()
    if not env.get("YIYI_WORKING_DIR"):
        env["YIYI_WORKING_DIR"] = tempfile.mkdtemp(prefix="yiyi_pdf_preprocess_")
    env["YIYI_DISABLE_AUTO_TEST"] = "1"

    print(f"[yiyi-pdf-preprocess] tasks_root={tasks_root}")
    print(f"[yiyi-pdf-preprocess] filesys_root={filesys_root}")
    stdout_path = out_dir / "preprocess_stdout.log"
    stderr_path = out_dir / "preprocess_stderr.log"

    print(f"[yiyi-pdf-preprocess] pdf_count={len(pdf_paths)}")
    print("[yiyi-pdf-preprocess] cmd=" + " ".join(cmd))
    print(f"[yiyi-pdf-preprocess] stdout_log={stdout_path}")
    print(f"[yiyi-pdf-preprocess] stderr_log={stderr_path}")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(args.cargo_root).resolve()),
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
        )
    print(f"[yiyi-pdf-preprocess] exit_code={proc.returncode}")
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
