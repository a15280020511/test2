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


def _run_soft(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, text=True, capture_output=True, env=os.environ.copy())


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and deliver a DeepSeek Steward repair if the operation produced one")
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
        print(f"DeepSeek Steward decision={decision or 'N/A'}; no repair delivery required.")
        return

    source_paths = ensure_safe_repair_changes()
    # Always verify again at the delivery boundary. Auto-repair already verified before retry,
    # but this second gate ensures only the exact workspace being delivered is accepted.
    run_verification()

    run_id = os.getenv("GITHUB_RUN_ID", "manual")
    branch = f"deepseek-repair-{operation_id}-{run_id}"
    run_checked(["git", "config", "user.name", "deepseek-steward[bot]"])
    run_checked(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_checked(["git", "checkout", "-b", branch])
    run_checked(["git", "add", "-A", "--", *source_paths])
    run_checked(["git", "commit", "-m", f"DeepSeek Steward repair {operation_id}"])
    run_checked(["git", "push", "origin", f"HEAD:{branch}"])

    pr_title = f"DeepSeek Steward repair: {operation_id}"
    pr_body = (
        "Automated DeepSeek Steward repair. The repair used the official DeepSeek API, "
        "passed Python compilation, existing unit tests, strict GPT Action OpenAPI validation, "
        "the live OpenRouter smoke test, and the live official DeepSeek smoke test. "
        "For automatically recovered production failures, the original operation also succeeded "
        "on the single allowed retry before delivery."
    )
    created = _run_soft(
        ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", pr_title, "--body", pr_body]
    )
    pr_url = created.stdout.strip() if created.returncode == 0 else ""
    delivery_method = "verified_direct_merge"

    if pr_url:
        merged = _run_soft(["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"])
        if merged.returncode == 0:
            delivery_method = "verified_pr_merge"
        else:
            # Never force push. The fallback succeeds only if GitHub accepts the normal update.
            run_checked(["git", "push", "origin", "HEAD:main"])
            _run_soft(
                [
                    "gh",
                    "pr",
                    "close",
                    pr_url,
                    "--comment",
                    "Repair was delivered by verified non-force fast-forward fallback because workflow PR merge was unavailable.",
                ]
            )
    else:
        run_checked(["git", "push", "origin", "HEAD:main"])

    result = read_json(result_path)
    result["repair_delivery"] = {
        "status": "merged",
        "method": delivery_method,
        "pull_request_url": pr_url or None,
        "verification": "passed",
        "changed_files": source_paths,
    }
    result["resume"] = "READY"
    write_json(result_path, result)

    if manifest_path.exists():
        manifest = read_json(manifest_path)
        manifest["status"] = "delivered"
        manifest["delivery_method"] = delivery_method
        manifest["pull_request_url"] = pr_url or None
        write_json(manifest_path, manifest)

    managed_path = output_dir / "managed_operation.json"
    if managed_path.exists():
        managed = read_json(managed_path)
        managed["delivery"] = "merged"
        managed["resume"] = "READY"
        write_json(managed_path, managed)

    print(f"DeepSeek repair delivered successfully via {delivery_method}")


if __name__ == "__main__":
    main()
