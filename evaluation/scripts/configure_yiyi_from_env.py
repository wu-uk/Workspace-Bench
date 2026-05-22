#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Dict
from urllib.parse import urlsplit, urlunsplit


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        raise SystemExit(f"missing env file: {path}")

    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.upper()] = os.path.expandvars(value.strip())
    return values


def _required(env: Dict[str, str], key: str) -> str:
    value = env.get(key.upper(), "").strip()
    if not value:
        raise SystemExit(f"{key} is empty in .env")
    return value


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_settings (
            provider_id TEXT PRIMARY KEY,
            api_key TEXT,
            base_url TEXT,
            extra_models TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS custom_providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            default_base_url TEXT NOT NULL DEFAULT '',
            api_key_prefix TEXT NOT NULL DEFAULT '',
            models TEXT NOT NULL DEFAULT '[]',
            is_local INTEGER NOT NULL DEFAULT 0,
            api_key TEXT,
            base_url TEXT
        );

        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _mask_secret(value: str) -> str:
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _normalize_openai_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if not path:
        return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
    if path.endswith("/chat/completions"):
        return urlunsplit((parsed.scheme, parsed.netloc, path[: -len("/chat/completions")], "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def configure(args: argparse.Namespace) -> Path:
    eval_root = Path(__file__).resolve().parents[1]
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = eval_root / env_path

    env = _parse_env_file(env_path)
    raw_base_url = _required(env, "JUDGE_BASE_URL")
    base_url = _normalize_openai_base_url(raw_base_url)
    model = _required(env, "JUDGE_MODEL")
    api_key = _required(env, "JUDGE_API_KEY")

    home_yiyi = Path(args.home_yiyi or os.environ.get("YIYI_HOME") or Path.home() / ".yiyi").expanduser()
    home_yiyi.mkdir(parents=True, exist_ok=True)
    db_path = home_yiyi / "yiyi.db"

    provider_id = args.provider_id
    provider_name = args.provider_name
    models_json = json.dumps([{"id": model, "name": model}], ensure_ascii=False)
    active_json = json.dumps({"provider_id": provider_id, "model": model}, ensure_ascii=False)

    with sqlite3.connect(str(db_path)) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO custom_providers
                (id, name, default_base_url, api_key_prefix, models, is_local, api_key, base_url)
            VALUES
                (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (provider_id, provider_name, base_url, "JUDGE_API_KEY", models_json, api_key, base_url),
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES ('active_llm', ?)",
            (active_json,),
        )
        conn.commit()

    print(f"[ok] YiYi model configured from {env_path}")
    print(f"     db: {db_path}")
    print(f"     active_llm: {provider_id} / {model}")
    print(f"     base_url: {base_url}")
    print(f"     api_key: {_mask_secret(api_key)}")
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure YiYi's headless eval model from Workspace-Bench/evaluation/.env judge settings."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env. Relative paths are resolved under Workspace-Bench/evaluation.",
    )
    parser.add_argument(
        "--home-yiyi",
        help="YiYi config directory to write. Defaults to $YIYI_HOME or ~/.yiyi.",
    )
    parser.add_argument(
        "--provider-id",
        default="workspace-bench-judge",
        help="Custom YiYi provider id to create/update.",
    )
    parser.add_argument(
        "--provider-name",
        default="Workspace-Bench Judge",
        help="Custom YiYi provider display name.",
    )
    args = parser.parse_args()
    configure(args)


if __name__ == "__main__":
    main()
