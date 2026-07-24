from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from scripts.repair_utils import (
    ensure_safe_repair_changes,
    read_json,
    run_checked,
    run_verification,
    safe_operation_id,
    write_json,
)

SENSITIVE_PREFIXES = (".github/workflows/",)
SENSITIVE_FILES = {
    "DEEPSEEK_STEWARD.md",
    "ACTION_RECOVERY.md",
    "TOOL_PACKAGE_GUARDRAILS.md",
    "requirements.txt",
    "requirements.in",
    "requirements.lock",
    "scripts/repair_utils.py",
    "scripts/repair_delivery.py",
    "scripts/validate_action_schema.rb",
    "scripts/single_task_lock.py",
    "scripts/cancel_operation.py",
    "gpt_action_openapi.yaml",
    "execution_plan.schema.json",
}


def _run_soft(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, text=True, capture_output=True, env=os.environ.copy())


def _requires_human_review(paths: list[str]) -> bool:
    return any(path in SENSITIVE_FILES or path.startswith(SENSITIVE_PREFIXES) for path in paths)


def _record_delivery(
    result_path: Path,
    *,
    status: str,
    method: str,
    branch: str,
    pr_url: str | None,
    changed_files: list[str],
    error: str | None = None,
    resume: str = "STOP",
) -> None:
    result = read_json(result_path)
    result["repair_delivery"] = {
        "status": status,
        "method": method,
        "branch": branch,
        "pull_request_url": pr_url,
        "verification": "passed",
        "changed_files": changed_files,
        "error": error,
    }
    result["resume"] = resume
    write_json(result_path, result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and deliver a DeepSeek Steward repair through a PR only")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()

    operation_id = safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    auto_result = output_dir / "auto_repair_result.json"
    direct_result = output_dir / "deepseek_steward_result.json"
    manifest_path = output_dir / "auto_repair_manifest.json"

    if auto_result.exists():
        result_path = auto_result
    elif direct_result.exists():
        result_path = direct_result
    else:
        print("No DeepSeek repair result for this operation; delivery is not required.")
        return

    result = read_json(result_path)
    decision = str(result.get("decision") or "").upper()
    if decision != "EDIT":
        print(f"DeepSeek Steward decision={decision or 'N/A'}; no repository repair delivery required.")
        return

    source_paths = ensure_safe_repair_changes()
    run_verification()

    run_id = os.getenv("GITHUB_RUN_ID", "manual")
    branch = f"deepseek-repair-{operation_id}-{run_id}"
    run_checked(["git", "config", "user.name", "deepseek-steward[bot]"])
    run_checked(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_checked(["git", "checkout", "-b", branch])
    run_checked(["git", "add", "-A", "--", *source_paths])
    run_checked(["git", "commit", "-m", f"DeepSeek Steward repair {operation_id}"])
    run_checked(["git", "push", "origin", f"HEAD:{branch}"])

    sensitive = _requires_human_review(source_paths)
    review_note = (
        " This repair changes security, workflow, budget, dependency, or control-plane files and requires human review."
        if sensitive
        else ""
    )
    pr_title = f"DeepSeek Steward repair: {operation_id}"
    pr_body = (
        "Automated DeepSeek Steward repair using the official DeepSeek API. "
        "The repair passed compilation, unit tests, strict Action-schema validation, "
        "OpenRouter smoke testing, and official DeepSeek smoke testing. "
        "Direct push to main is forbidden; this PR is the only delivery path." + review_note
    )
    created = _run_soft(
        ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", pr_title, "--body", pr_body]
    )
    pr_url = created.stdout.strip() if created.returncode == 0 else ""
    if not pr_url:
        _record_delivery(
            result_path,
            status="blocked",
            method="verified_branch_only",
            branch=branch,
            pr_url=None,
            changed_files=source_paths,
            error=(created.stderr or created.stdout)[-4000:],
        )
        raise RuntimeError("Verified repair branch was pushed, but PR creation failed; direct main push is forbidden")

    if sensitive:
        _record_delivery(
            result_path,
            status="pending_human_review",
            method="verified_pr_human_review_required",
            branch=branch,
            pr_url=pr_url,
            changed_files=source_paths,
            error=None,
        )
        raise RuntimeError("Verified repair PR changes protected control files and requires human review")

    merged = _run_soft(["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"])
    if merged.returncode != 0:
        _record_delivery(
            result_path,
            status="pending_review",
            method="verified_pr",
            branch=branch,
            pr_url=pr_url,
            changed_files=source_paths,
            error=(merged.stderr or merged.stdout)[-4000:],
        )
        raise RuntimeError("Verified repair PR could not be merged automatically; direct main push is forbidden")

    _record_delivery(
        result_path,
        status="merged",
        method="verified_pr_merge",
        branch=branch,
        pr_url=pr_url,
        changed_files=source_paths,
        resume="READY",
    )

    if manifest_path.exists():
        manifest = read_json(manifest_path)
        manifest["status"] = "delivered"
        manifest["delivery_method"] = "verified_pr_merge"
        manifest["pull_request_url"] = pr_url
        write_json(manifest_path, manifest)

    managed_path = output_dir / "managed_operation.json"
    if managed_path.exists():
        managed = read_json(managed_path)
        managed["delivery"] = "merged"
        managed["resume"] = "READY"
        write_json(managed_path, managed)

    print("DeepSeek repair delivered successfully via verified PR merge")


if __name__ == "__main__":
    main()
