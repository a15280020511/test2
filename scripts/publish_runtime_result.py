from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from scripts.repair_utils import run_checked, safe_operation_id

PUBLISH_FILES = (
    "metadata.json",
    "expert_team_result.json",
    "deepseek_steward_result.json",
    "auto_repair_result.json",
    "auto_repair_manifest.json",
    "managed_operation.json",
    "model_intelligence_gpt.json",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish GPT-readable runtime JSON to the runtime-results branch")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()

    operation_id = safe_operation_id(args.operation_id)
    source_dir = Path("artifacts") / operation_id
    if not source_dir.exists():
        print("No operation artifact directory exists; nothing to publish.")
        return

    run_checked(["git", "config", "user.name", "github-actions[bot]"])
    run_checked(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_checked(["git", "fetch", "origin", "runtime-results"])

    with tempfile.TemporaryDirectory(prefix="test2-runtime-results-") as tmp:
        worktree = Path(tmp) / "worktree"
        run_checked(["git", "worktree", "add", str(worktree), "origin/runtime-results"])
        try:
            destination = worktree / "runtime_results" / operation_id
            destination.mkdir(parents=True, exist_ok=True)

            for filename in PUBLISH_FILES:
                source = source_dir / filename
                if source.exists():
                    target_name = "model_intelligence.json" if filename == "model_intelligence_gpt.json" else filename
                    shutil.copy2(source, destination / target_name)

            gpt_snapshot = source_dir / "model_intelligence_gpt.json"
            if gpt_snapshot.exists():
                # Keep the existing public Action path for backward compatibility, but
                # publish only the new bounded/minified GPT snapshot there.
                for latest_name in (
                    "model_intelligence_latest.json",
                    "model_intelligence_gpt_latest.json",
                ):
                    latest = worktree / "runtime_results" / latest_name
                    latest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(gpt_snapshot, latest)

            run_checked(["git", "-C", str(worktree), "add", "runtime_results"])
            diff = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--cached", "--quiet"],
                check=False,
                text=True,
                capture_output=True,
            )
            if diff.returncode == 0:
                print("No runtime result changes to publish")
                return
            run_checked(["git", "-C", str(worktree), "commit", "-m", f"Publish runtime result {operation_id}"])
            run_checked(["git", "-C", str(worktree), "push", "origin", "HEAD:runtime-results"])
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=False)


if __name__ == "__main__":
    main()
