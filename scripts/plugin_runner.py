from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = ROOT / "plugins"
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class PluginError(RuntimeError):
    pass


def _safe_name(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or not SAFE_NAME.fullmatch(cleaned):
        raise PluginError(f"{field} contains unsafe characters")
    return cleaned


def _load_manifest(plugin: str) -> tuple[Path, dict[str, Any]]:
    plugin_dir = PLUGINS_ROOT / _safe_name(plugin, "plugin")
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise PluginError(f"unknown plugin: {plugin}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("name") != plugin:
        raise PluginError("plugin manifest is invalid")
    return plugin_dir, payload


def _positive_int(value: Any, field: str, default: int) -> int:
    raw = default if value is None else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise PluginError(f"{field} must be an integer") from exc
    if parsed < 1:
        raise PluginError(f"{field} must be positive")
    return parsed


def _python_path(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install, run, and destroy one task-scoped plugin")
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    plugin_dir, manifest = _load_manifest(args.plugin)
    operation_id = _safe_name(args.operation_id, "operation_id")[:120]
    operation = _safe_name(args.operation, "operation")
    module = args.module.strip()

    allowed_operations = manifest.get("allowed_operations")
    allowed_modules = manifest.get("allowed_modules")
    if not isinstance(allowed_operations, list) or operation not in allowed_operations:
        raise PluginError(f"operation {operation!r} is not allowed for plugin {args.plugin!r}")
    if not isinstance(allowed_modules, list) or module not in allowed_modules:
        raise PluginError(f"module {module!r} is not allowed for plugin {args.plugin!r}")

    requirements_name = str(manifest.get("requirements_file") or "requirements.txt")
    requirements_path = plugin_dir / requirements_name
    if requirements_path.parent != plugin_dir or not requirements_path.is_file():
        raise PluginError("plugin requirements file is invalid")

    temp_parent = os.getenv("RUNNER_TEMP") or None
    temp_root = Path(tempfile.mkdtemp(prefix=f"test2-{args.plugin}-{operation_id}-", dir=temp_parent))
    venv_dir = temp_root / "venv"
    audit_path = ROOT / "artifacts" / operation_id / f"plugin-{args.plugin}-lifecycle.json"
    install_log = ROOT / "artifacts" / operation_id / f"plugin-{args.plugin}-install.log"
    started = time.time()
    audit: dict[str, Any] = {
        "schema_version": 1,
        "plugin": args.plugin,
        "operation": operation,
        "operation_id": operation_id,
        "status": "initializing",
        "temporary_environment": True,
        "cleanup_policy": "always",
        "created_at_epoch": int(started),
        "cleaned": False,
    }
    _write_json(audit_path, audit)

    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = _python_path(venv_dir)
        install_timeout = _positive_int(manifest.get("install_timeout_seconds"), "install_timeout_seconds", 900)
        run_timeout = _positive_int(manifest.get("run_timeout_seconds"), "run_timeout_seconds", 3600)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ROOT),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "TEST2_ACTIVE_PLUGIN": args.plugin,
                "TEST2_PLUGIN_OPERATION_ID": operation_id,
            }
        )

        install_log.parent.mkdir(parents=True, exist_ok=True)
        with install_log.open("w", encoding="utf-8") as handle:
            install = subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(requirements_path)],
                cwd=ROOT,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=install_timeout,
                check=False,
            )
        if install.returncode != 0:
            raise PluginError(f"plugin dependency installation failed with exit code {install.returncode}")

        remainder = list(args.arguments)
        if remainder and remainder[0] == "--":
            remainder = remainder[1:]
        audit.update({"status": "running", "installed": True})
        _write_json(audit_path, audit)
        completed = subprocess.run(
            [str(python), "-m", module, *remainder],
            cwd=ROOT,
            env=env,
            timeout=run_timeout,
            check=False,
        )
        audit.update(
            {
                "status": "success" if completed.returncode == 0 else "failure",
                "returncode": completed.returncode,
            }
        )
        return completed.returncode
    except subprocess.TimeoutExpired as exc:
        audit.update({"status": "timeout", "error": str(exc)})
        return 124
    except Exception as exc:  # noqa: BLE001
        audit.update({"status": "failure", "error_type": type(exc).__name__, "error": str(exc)})
        return 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
        audit.update(
            {
                "cleaned": not temp_root.exists(),
                "finished_at_epoch": int(time.time()),
                "duration_seconds": round(time.time() - started, 3),
            }
        )
        _write_json(audit_path, audit)


if __name__ == "__main__":
    raise SystemExit(main())
