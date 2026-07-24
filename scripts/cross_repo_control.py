from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

API = "https://api.github.com"
TARGET_REPOSITORY = "a15280020511/test"
TARGET_WORKFLOW = "think-tank.yml"
CONTROL_REPOSITORY = "a15280020511/test2"
ACTIVE_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}
TERMINAL_STATES = {"completed"}
ALLOWED_REPAIR_PREFIXES = (
    ".github/workflows/",
    "scripts/",
    "tests/",
    "prompts/",
)
ALLOWED_REPAIR_FILES = {
    "README.md",
    "gpt-action-openapi.yaml",
    "runtime_plugs.yml",
    ".yamllint.yml",
}
CONTEXT_PATHS = (
    "README.md",
    "gpt-action-openapi.yaml",
    "runtime_plugs.yml",
    "prompts/DEEPSEEK_SYSTEM.md",
    ".github/workflows/think-tank.yml",
    ".github/workflows/deepseek-support.yml",
    ".github/workflows/validate.yml",
    "scripts/execution_plan.py",
    "scripts/input_normalizer.py",
    "scripts/universal_tool_runner.py",
    "scripts/deepseek_support.py",
    "scripts/verify_control_ticket.py",
    "scripts/publish_report.py",
    "scripts/validate_repository.sh",
    "scripts/full_repository_hygiene.py",
    "tests/unit/test_workflow_boundaries.py",
    "tests/unit/test_execution_plan.py",
    "tests/unit/test_control_ticket.py",
)


class ControlError(RuntimeError):
    """Raised when a control-plane operation cannot progress safely."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class GitHubClient:
    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ControlError("CONTROL_PLANE_TOKEN is not configured")
        self.token = token.strip()
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        binary: bool = False,
        allow_404: bool = False,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "deepseek-independent-control-plane",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read()
                return self._decode(raw, response.status, binary)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location", "")
                if not location:
                    raise ControlError(f"GitHub redirect for {path} had no Location header") from exc
                redirect = urllib.request.Request(
                    location,
                    headers={"Accept": "*/*", "User-Agent": "deepseek-independent-control-plane"},
                    method="GET",
                )
                with urllib.request.urlopen(redirect, timeout=180) as response:
                    return self._decode(response.read(), response.status, binary)
            body = exc.read().decode("utf-8", errors="replace")
            raise ControlError(
                f"GitHub API {method} {path} failed: HTTP {exc.code}: {body[:4000]}"
            ) from exc

    @staticmethod
    def _decode(raw: bytes, status: int, binary: bool) -> Any:
        if binary:
            return raw
        if not raw:
            return {"http_status": status}
        return json.loads(raw.decode("utf-8"))


class DeepSeekClient:
    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ControlError("DEEPSEEK_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = self._discover_model()

    def _json_request(self, url: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ControlError(f"DeepSeek request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise ControlError("DeepSeek returned non-object JSON")
        return result

    @staticmethod
    def _version(model_id: str) -> tuple[int, ...]:
        return tuple(int(value) for value in re.findall(r"\d+", model_id.lower()))

    def _discover_model(self) -> str:
        data = self._json_request("https://api.deepseek.com/models").get("data")
        if not isinstance(data, list):
            raise ControlError("DeepSeek /models response requires data[]")
        candidates = []
        for item in data:
            model_id = str(item.get("id", "")).strip()
            owner = str(item.get("owned_by", "")).strip().lower()
            if model_id and (owner == "deepseek" or model_id.lower().startswith("deepseek")):
                candidates.append(model_id)
        if not candidates:
            raise ControlError("DeepSeek /models returned no usable model")
        premium = [m for m in candidates if any(t in m.lower() for t in ("pro", "max", "ultra", "expert"))]
        non_light = [m for m in candidates if not any(t in m.lower() for t in ("flash", "lite", "mini"))]
        return max(premium or non_light or candidates, key=lambda value: (self._version(value), value.lower()))

    def call(self, *, purpose: str, packet: dict[str, Any], repair: bool = False) -> dict[str, Any]:
        if repair:
            contract = (
                "Return JSON only with decision REPAIR or STOP; summary; root_cause; evidence[]; "
                "repository_edits[]; validation_requirements[]; and confidence. Each repository edit must be "
                "{path, action: write|delete, content}. Use complete file contents for write. Keep changes minimal."
            )
        else:
            contract = (
                "Return JSON only with decision APPROVE, REPLAN, COLLECT, REPAIR, or STOP; summary; defects[]; "
                "required_actions[]; and confidence."
            )
        prompt = (
            "You are the independent highest-priority control authority for a15280020511/test. "
            f"Perform {purpose}. Distinguish verified facts, assumptions, code output, and uncertainty. "
            "Do not fabricate repository state, logs, evidence, execution success, or citations. "
            + contract
        )
        response = self._json_request(
            "https://api.deepseek.com/chat/completions",
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(packet, ensure_ascii=False, sort_keys=True)},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ControlError("DeepSeek response requires choices[]")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ControlError("DeepSeek returned empty content")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ControlError(f"DeepSeek response was not valid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise ControlError("DeepSeek response must be an object")
        result["runtime_model"] = self.model
        return result


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(output_dir: Path, name: str, payload: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def matching_runs(github: GitHubClient, task_id: str, revision: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"event": "workflow_dispatch", "per_page": 100})
    data = github.request(
        "GET",
        f"/repos/{TARGET_REPOSITORY}/actions/workflows/{TARGET_WORKFLOW}/runs?{query}",
    )
    expected = f"Think Tank · {task_id} · r{revision}"
    return [
        item
        for item in data.get("workflow_runs", [])
        if str(item.get("display_title", "")).startswith(expected)
    ]


def wait_for_run(github: GitHubClient, task_id: str, revision: int, timeout_seconds: int = 180) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        matches = matching_runs(github, task_id, revision)
        if matches:
            return matches[0]
        time.sleep(10)
    raise ControlError("dispatched Think Tank run did not appear within three minutes")


def monitor_run(github: GitHubClient, run_id: int, timeout_seconds: int = 10800) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = github.request("GET", f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}")
        if run.get("status") in TERMINAL_STATES:
            return run
        time.sleep(30)
    github.request("POST", f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}/force-cancel")
    raise ControlError("target run exceeded three-hour control-plane monitor limit and was force-cancelled")


def run_packet(github: GitHubClient, run_id: int, *, include_artifact: bool, include_logs: bool) -> dict[str, Any]:
    run = github.request("GET", f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}")
    jobs = github.request("GET", f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100")
    packet: dict[str, Any] = {"run": run, "jobs": jobs}
    if include_logs:
        logs: dict[str, str] = {}
        for job in jobs.get("jobs", [])[:10]:
            try:
                raw = github.request(
                    "GET",
                    f"/repos/{TARGET_REPOSITORY}/actions/jobs/{job['id']}/logs",
                    binary=True,
                )
                logs[str(job["id"])] = raw.decode("utf-8", errors="replace")[-60000:]
            except Exception as exc:  # noqa: BLE001
                logs[str(job.get("id"))] = f"log retrieval failed: {exc}"
        packet["job_log_tails"] = logs
    if include_artifact:
        artifacts = github.request(
            "GET",
            f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
        )
        packet["artifacts"] = artifacts
        selected = next((item for item in artifacts.get("artifacts", []) if not item.get("expired")), None)
        if selected:
            archive = github.request(
                "GET",
                f"/repos/{TARGET_REPOSITORY}/actions/artifacts/{selected['id']}/zip",
                binary=True,
            )
            if len(archive) > 25 * 1024 * 1024:
                raise ControlError("artifact exceeds 25 MiB control-plane review limit")
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                files: dict[str, str] = {}
                preferred = [
                    name
                    for name in zf.namelist()
                    if name.endswith(("report.json", "report.md", "provenance.json", "manifest.sha256", "result.json"))
                ]
                for name in preferred[:16]:
                    files[name] = zf.read(name).decode("utf-8", errors="replace")[:150000]
                packet["artifact_files"] = files
    return packet


def validate_repair_edits(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ControlError("DeepSeek REPAIR requires non-empty repository_edits[]")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ControlError(f"repository_edits[{index}] must be an object")
        value = str(item.get("path", "")).strip().replace("\\", "/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or value.startswith(".git/"):
            raise ControlError(f"invalid repair path: {value!r}")
        if value not in ALLOWED_REPAIR_FILES and not value.startswith(ALLOWED_REPAIR_PREFIXES):
            raise ControlError(f"repair path is outside the controlled code surface: {value}")
        if value in seen:
            raise ControlError(f"duplicate repair path: {value}")
        seen.add(value)
        action = str(item.get("action", "write")).strip().lower()
        if action not in {"write", "delete"}:
            raise ControlError(f"invalid repair action for {value}: {action}")
        content = item.get("content", "")
        if action == "write" and not isinstance(content, str):
            raise ControlError(f"repair content for {value} must be text")
        result.append({"path": value, "action": action, "content": content if isinstance(content, str) else ""})
    return result


def repository_context(github: GitHubClient) -> dict[str, str]:
    files: dict[str, str] = {}
    used = 0
    for path in CONTEXT_PATHS:
        encoded = urllib.parse.quote(path, safe="/")
        item = github.request(
            "GET",
            f"/repos/{TARGET_REPOSITORY}/contents/{encoded}?ref=main",
            allow_404=True,
        )
        if not item or item.get("type") != "file":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            text = base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if used + len(text) > 500000:
            break
        files[path] = text
        used += len(text)
    return files


def create_repair_pr(
    github: GitHubClient,
    *,
    task_id: str,
    run_id: int,
    repair_result: dict[str, Any],
) -> dict[str, Any]:
    edits = validate_repair_edits(repair_result.get("repository_edits"))
    ref = github.request("GET", f"/repos/{TARGET_REPOSITORY}/git/ref/heads/main")
    base_sha = str(ref.get("object", {}).get("sha", ""))
    if not base_sha:
        raise ControlError("could not resolve target main SHA")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-")[:40] or "task"
    branch = f"deepseek-repair/{safe_task}-{run_id}-{int(time.time())}"
    github.request(
        "POST",
        f"/repos/{TARGET_REPOSITORY}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )

    changed: list[str] = []
    for edit in edits:
        encoded = urllib.parse.quote(edit["path"], safe="/")
        existing = github.request(
            "GET",
            f"/repos/{TARGET_REPOSITORY}/contents/{encoded}?ref={urllib.parse.quote(branch)}",
            allow_404=True,
        )
        if edit["action"] == "delete":
            if not existing:
                continue
            github.request(
                "DELETE",
                f"/repos/{TARGET_REPOSITORY}/contents/{encoded}",
                {
                    "message": f"DeepSeek repair: delete {edit['path']}",
                    "sha": existing["sha"],
                    "branch": branch,
                },
            )
        else:
            payload: dict[str, Any] = {
                "message": f"DeepSeek repair: update {edit['path']}",
                "content": base64.b64encode(edit["content"].encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if existing:
                payload["sha"] = existing["sha"]
            github.request("PUT", f"/repos/{TARGET_REPOSITORY}/contents/{encoded}", payload)
        changed.append(edit["path"])

    if not changed:
        raise ControlError("DeepSeek REPAIR produced no repository diff")
    summary = str(repair_result.get("summary", "Validated DeepSeek repair proposal"))
    body = (
        "## Independent DeepSeek repair\n\n"
        f"Source failed Run: `{run_id}`\n\n"
        f"Summary: {summary}\n\n"
        "### Changed files\n\n"
        + "\n".join(f"- `{path}`" for path in changed)
        + "\n\nThis PR is intentionally not auto-merged. Repository Validation and Think Tank self-test must pass before merge."
    )
    pr = github.request(
        "POST",
        f"/repos/{TARGET_REPOSITORY}/pulls",
        {
            "title": f"DeepSeek repair for Run {run_id}",
            "head": branch,
            "base": "main",
            "body": body,
            "maintainer_can_modify": True,
        },
    )
    return {"branch": branch, "base_sha": base_sha, "changed_files": changed, "pull_request": pr}


def ticket(
    *,
    secret: str,
    task_id: str,
    revision: int,
    objective: str,
    task_json: str,
    evidence_json: str,
    plan_json: str,
) -> dict[str, Any]:
    if not secret.strip():
        raise ControlError("CONTROL_TICKET_SECRET is not configured")
    now = int(time.time())
    value: dict[str, Any] = {
        "schema_version": 1,
        "issuer": CONTROL_REPOSITORY,
        "target_repo": TARGET_REPOSITORY,
        "workflow": TARGET_WORKFLOW,
        "task_id": task_id,
        "revision": revision,
        "objective_sha256": sha256(objective),
        "task_sha256": sha256(task_json),
        "evidence_sha256": sha256(evidence_json),
        "plan_sha256": sha256(plan_json),
        "issued_at": now,
        "expires_at": now + 3600,
        "nonce": uuid.uuid4().hex,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["signature"] = hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return value


def parse_revision(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ControlError("revision must be a positive integer") from exc
    if value < 1:
        raise ControlError("revision must be a positive integer")
    return value


def parse_context(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ControlError(f"support_context_json is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError("support_context_json must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--revision", default="1")
    parser.add_argument("--task-objective", default="")
    parser.add_argument("--task-json", default="{}")
    parser.add_argument("--evidence-json", default="{}")
    parser.add_argument("--execution-plan-json", default="{}")
    parser.add_argument("--target-run-id", default="")
    parser.add_argument("--support-context-json", default="{}")
    parser.add_argument("--target-repo", default=TARGET_REPOSITORY)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.target_repo != TARGET_REPOSITORY:
        raise ControlError(f"refusing uncontrolled target repository: {args.target_repo}")
    operation = args.operation.upper()
    revision = parse_revision(args.revision)
    github = GitHubClient(os.environ.get("CONTROL_PLANE_TOKEN", ""))
    context = parse_context(args.support_context_json)
    output: dict[str, Any] = {
        "operation": operation,
        "task_id": args.task_id,
        "revision": revision,
        "target_repo": TARGET_REPOSITORY,
        "generated_at": int(time.time()),
    }

    if operation in {"START", "RESTART"}:
        if not args.task_objective.strip():
            raise ControlError("task_objective is required for START or RESTART")
        if operation == "RESTART" and args.target_run_id.strip():
            if not args.target_run_id.isdigit():
                raise ControlError("target_run_id must be an integer")
            github.request(
                "POST",
                f"/repos/{TARGET_REPOSITORY}/actions/runs/{int(args.target_run_id)}/force-cancel",
            )
        matches = matching_runs(github, args.task_id, revision)
        active = [item for item in matches if item.get("status") in ACTIVE_STATES]
        successful = [
            item
            for item in matches
            if item.get("status") == "completed" and item.get("conclusion") == "success"
        ]
        if active:
            target = active[0]
            output["dispatch_status"] = "DUPLICATE_ACTIVE"
        elif successful and operation == "START":
            target = successful[0]
            output["dispatch_status"] = "DUPLICATE_COMPLETED"
        else:
            signed = ticket(
                secret=os.environ.get("CONTROL_TICKET_SECRET", ""),
                task_id=args.task_id,
                revision=revision,
                objective=args.task_objective,
                task_json=args.task_json,
                evidence_json=args.evidence_json,
                plan_json=args.execution_plan_json,
            )
            github.request(
                "POST",
                f"/repos/{TARGET_REPOSITORY}/actions/workflows/{TARGET_WORKFLOW}/dispatches",
                {
                    "ref": "main",
                    "inputs": {
                        "task_id": args.task_id,
                        "task_objective": args.task_objective,
                        "task_json": args.task_json,
                        "evidence_json": args.evidence_json,
                        "execution_plan_json": args.execution_plan_json,
                        "task_json_b64": "",
                        "evidence_json_b64": "",
                        "execution_plan_b64": "",
                        "revision": str(revision),
                        "control_ticket_json": json.dumps(
                            signed, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                },
            )
            target = wait_for_run(github, args.task_id, revision)
            output["dispatch_status"] = "DISPATCH_ACCEPTED"
            output["control_ticket"] = signed

        run_id = int(target["id"])
        output["target_run_id"] = run_id
        terminal = monitor_run(github, run_id)
        output["target_run"] = terminal
        deepseek = DeepSeekClient(os.environ.get("DEEPSEEK_API_KEY", ""))
        if terminal.get("conclusion") == "success":
            packet = run_packet(github, run_id, include_artifact=True, include_logs=False)
            packet.update({"task_id": args.task_id, "revision": revision, "support_context": context})
            decision = deepseek.call(
                purpose="a final result-quality and publication review",
                packet=packet,
            )
            output["deepseek_review"] = decision
            output["status"] = "APPROVED" if decision.get("decision") == "APPROVE" else "REVIEW_BLOCKED"
        else:
            packet = run_packet(github, run_id, include_artifact=True, include_logs=True)
            packet.update({"task_id": args.task_id, "revision": revision, "support_context": context})
            decision = deepseek.call(
                purpose="a failed-run diagnosis and smallest safe recovery decision",
                packet=packet,
            )
            output["deepseek_diagnosis"] = decision
            output["status"] = "TARGET_FAILED"

    elif operation in {"CANCEL", "FORCE_CANCEL"}:
        if not args.target_run_id.isdigit():
            raise ControlError("target_run_id must be supplied as an integer")
        run_id = int(args.target_run_id)
        endpoint = "force-cancel" if operation == "FORCE_CANCEL" else "cancel"
        response = github.request(
            "POST", f"/repos/{TARGET_REPOSITORY}/actions/runs/{run_id}/{endpoint}"
        )
        output.update({"status": f"{operation}_REQUESTED", "target_run_id": run_id, "github": response})

    elif operation == "STATUS":
        if args.target_run_id.strip():
            if not args.target_run_id.isdigit():
                raise ControlError("target_run_id must be an integer")
            run = github.request(
                "GET", f"/repos/{TARGET_REPOSITORY}/actions/runs/{int(args.target_run_id)}"
            )
            output.update({"status": "STATUS_READ", "target_run": run})
        else:
            output.update({"status": "STATUS_READ", "matching_runs": matching_runs(github, args.task_id, revision)})

    elif operation in {"REVIEW", "DIAGNOSE", "REPAIR"}:
        if not args.target_run_id.isdigit():
            raise ControlError("target_run_id must be supplied as an integer")
        run_id = int(args.target_run_id)
        deepseek = DeepSeekClient(os.environ.get("DEEPSEEK_API_KEY", ""))
        packet = run_packet(
            github,
            run_id,
            include_artifact=True,
            include_logs=operation in {"DIAGNOSE", "REPAIR"},
        )
        packet.update({"task_id": args.task_id, "revision": revision, "support_context": context})
        if operation == "REVIEW":
            decision = deepseek.call(
                purpose="a final result-quality and publication review",
                packet=packet,
            )
            output.update(
                {
                    "status": "APPROVED" if decision.get("decision") == "APPROVE" else "REVIEW_BLOCKED",
                    "target_run_id": run_id,
                    "deepseek_review": decision,
                }
            )
        elif operation == "DIAGNOSE":
            decision = deepseek.call(
                purpose="a workflow diagnosis and smallest safe recovery decision",
                packet=packet,
            )
            output.update(
                {"status": "DIAGNOSIS_COMPLETED", "target_run_id": run_id, "deepseek_diagnosis": decision}
            )
        else:
            packet["repository_context"] = repository_context(github)
            decision = deepseek.call(
                purpose="a repository repair proposal grounded in the failed Run and current repository files",
                packet=packet,
                repair=True,
            )
            if decision.get("decision") != "REPAIR":
                output.update(
                    {"status": "REPAIR_STOPPED", "target_run_id": run_id, "deepseek_repair": decision}
                )
            else:
                delivery = create_repair_pr(
                    github,
                    task_id=args.task_id,
                    run_id=run_id,
                    repair_result=decision,
                )
                output.update(
                    {
                        "status": "REPAIR_PR_CREATED",
                        "target_run_id": run_id,
                        "deepseek_repair": decision,
                        "repair_delivery": delivery,
                    }
                )
    else:
        raise ControlError(f"unsupported operation: {operation}")

    write_json(args.output_dir, "control-result.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if output.get("status") in {"REVIEW_BLOCKED", "TARGET_FAILED", "REPAIR_STOPPED"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
