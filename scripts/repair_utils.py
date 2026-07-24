from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterable

PROTECTED_SOURCE_PREFIXES = ("tests/", "runtime_results/", ".git/")
IGNORED_GENERATED_PREFIXES = ("artifacts/", "__pycache__/", ".pytest_cache/")


def safe_operation_id(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip() if ch.isalnum() or ch in {"-", "_", "."})
    if not cleaned:
        raise ValueError("operation_id must contain at least one safe character")
    return cleaned[:120]


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout[-8000:]}\nSTDERR:\n{completed.stderr[-8000:]}"
        )
    return completed


def changed_paths() -> list[str]:
    completed = run_checked(["git", "status", "--porcelain=v1", "--untracked-files=all"])
    paths: list[str] = []
    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        path = raw_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.replace("\\", "/")
        if normalized.endswith(".pyc") or any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in IGNORED_GENERATED_PREFIXES
        ):
            continue
        paths.append(normalized)
    return paths


def repair_source_paths(paths: Iterable[str] | None = None) -> list[str]:
    source_paths: list[str] = []
    for path in paths or changed_paths():
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_SOURCE_PREFIXES):
            continue
        source_paths.append(path)
    return source_paths


def ensure_safe_repair_changes() -> list[str]:
    paths = changed_paths()
    protected = [
        path
        for path in paths
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_SOURCE_PREFIXES)
    ]
    if protected:
        raise RuntimeError(f"DeepSeek Steward attempted to modify protected source paths: {protected}")
    source_paths = repair_source_paths(paths)
    if not source_paths:
        raise RuntimeError("DeepSeek Steward returned EDIT but produced no repository source/configuration changes")
    return source_paths


def run_verification() -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", ".")
    run_checked(["python", "-m", "compileall", "-q", "expert_team", "scripts"], env=env)
    run_checked(["python", "-m", "unittest", "discover", "-s", "tests", "-v"], env=env)
    run_checked(["ruby", "scripts/validate_action_schema.rb"], env=env)
    run_checked(["python", "scripts/live_smoke_test.py"], env=env)
    run_checked(["python", "scripts/deepseek_steward_smoke_test.py"], env=env)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
