from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any


def _load_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"raw_support_packet": raw}
    return value if isinstance(value, dict) else {"raw_support_packet": raw}


def _request(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _request_json(url: str, token: str) -> dict[str, Any]:
    raw = _request(url, token).decode("utf-8")
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def enrich(packet: dict[str, Any], failed_run_id: str, repository: str, token: str) -> dict[str, Any]:
    if not failed_run_id:
        return packet

    evidence: dict[str, Any] = {"run_id": failed_run_id, "jobs": [], "log_excerpts": {}}
    try:
        run = _request_json(f"https://api.github.com/repos/{repository}/actions/runs/{failed_run_id}", token)
        evidence["run"] = {
            key: run.get(key)
            for key in ("id", "name", "display_title", "status", "conclusion", "event", "run_number", "html_url")
        }

        jobs_payload = _request_json(
            f"https://api.github.com/repos/{repository}/actions/runs/{failed_run_id}/jobs?filter=latest&per_page=100",
            token,
        )
        jobs = jobs_payload.get("jobs", [])
        if isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                job_id = job.get("id")
                summary = {
                    "id": job_id,
                    "name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "steps": [
                        {
                            "name": step.get("name"),
                            "status": step.get("status"),
                            "conclusion": step.get("conclusion"),
                            "number": step.get("number"),
                        }
                        for step in (job.get("steps") or [])
                        if isinstance(step, dict)
                    ],
                }
                evidence["jobs"].append(summary)
                if job_id and job.get("conclusion") == "failure":
                    try:
                        log_bytes = _request(
                            f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs",
                            token,
                        )
                        log_text = log_bytes.decode("utf-8", errors="replace")
                        evidence["log_excerpts"][str(job_id)] = log_text[-16000:]
                    except Exception as exc:  # Evidence enrichment must not block supervision.
                        evidence["log_excerpts"][str(job_id)] = f"log fetch failed: {type(exc).__name__}: {exc}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        evidence["collection_error"] = f"{type(exc).__name__}: {exc}"

    merged = dict(packet)
    merged["github_failure_evidence"] = evidence
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich a DeepSeek supervisor Support Packet with GitHub Run evidence")
    parser.add_argument("--support-packet-json", required=True)
    parser.add_argument("--failed-run-id", default="")
    args = parser.parse_args()

    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    repository = (os.getenv("GITHUB_REPOSITORY") or "a15280020511/test2").strip()
    packet = _load_object(args.support_packet_json)

    if args.failed_run_id and token:
        packet = enrich(packet, args.failed_run_id.strip(), repository, token)
    elif args.failed_run_id:
        packet["github_failure_evidence"] = {
            "run_id": args.failed_run_id.strip(),
            "collection_error": "GitHub token unavailable for evidence enrichment",
        }

    print(json.dumps(packet, ensure_ascii=False))


if __name__ == "__main__":
    main()
