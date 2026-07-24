# Automatic DeepSeek Recovery and Operation Status Policy

## Purpose

`a15280020511/test2` uses DeepSeek Steward as the mandatory repository repair manager and a small `operation_id`-keyed status file as the normal control plane.

The control plane and audit plane are intentionally separated:

- **Control plane**: `runtime_results/status/<operation_id>.json`.
- **Audit plane**: Run, Job, Step, logs, Artifact, and detailed runtime-result files.

Normal task flow must not use a workflow-run list to discover or track a task.

## Operation status control plane

After Web GPT receives HTTP `204` from `dispatchExpertTeamOperation`:

1. Poll `getOperationStatus(operation_id)` with `ref=runtime-results`.
2. A temporary `404` immediately after an accepted dispatch means the workflow is queued or has not yet published its initial status. It is **not** a repair trigger by itself.
3. When the workflow starts, it publishes a small status object containing `operation_id`, `operation`, `status`, `run_id`, `result_ready`, `repair_status`, and `updated_at`.
4. While `status=running`, continue waiting. Do not read the final result yet.
5. When `status=success` and `result_ready=true`, read the operation-specific result file.
6. When `status=STOP`, end the current task and report the preserved evidence. Do not use another provider in place of DeepSeek Steward.
7. When `status=failure`, treat the workflow/control-plane execution as failed and use the available status `run_id` for audit and diagnosis.

`listExpertTeamRuns` is not part of normal control flow. Use the single `run_id` from the status object for optional Run/Job/Step/Artifact audit reads.

## GitHub-internal automatic recovery

For `model_intelligence` and `execute_team`:

1. Run the requested operation normally.
2. If it succeeds, finish normally.
3. If it fails, automatically create an evidence-based Support Packet from the operation, error output, and available context.
4. Automatically invoke `DeepSeek Steward` in `REPAIR` mode through the official DeepSeek API.
5. If DeepSeek is unreachable, authentication fails, official model discovery fails, or DeepSeek inference fails, immediately `STOP` the current operation. Never use OpenRouter or another provider as a Steward fallback.
6. If Steward returns `EDIT`, apply only its bounded repository edits, run the mandatory verification gate, and retry the original operation exactly once in the repaired workspace.
7. Deliver the verified repair to the repository only when verification and the single retry both succeed.
8. If the retry fails, `STOP`; do not start an infinite repair loop.
9. If Steward returns `NO_EDIT` or `STOP`, end the current operation and preserve the evidence.

The automatic repair cycle is limited to **one repair attempt and one retry per original operation**.

## Web GPT Action-edge recovery

A workflow cannot directly observe an HTTP/client failure that occurs before a workflow is dispatched. Therefore Web GPT must distinguish a true Action-edge failure from a normal queued state.

Automatic DeepSeek REPAIR triggers include:

- GitHub REST `4xx` or `5xx` from a required test2 Action operation, except the documented temporary status-file `404` after an accepted dispatch;
- a required Action response that is empty or cannot be parsed;
- Execution Plan Schema cannot be read or parsed;
- a final result is missing after `getOperationStatus` reports `status=success` and `result_ready=true`;
- the same required read still fails after its documented refresh step.

Mandatory protocol:

1. Do not improvise a repository code fix in Web GPT.
2. Build a Support Packet from the failed call and available evidence.
3. Dispatch `operation=deepseek_steward` with `steward_mode=REPAIR`.
4. Track that repair operation through `getOperationStatus`, not through a workflow-run list.
5. Read the Steward result only after the repair operation reaches a terminal status.
6. Resume the original task only when the Steward result says `READY`.
7. If the official DeepSeek API is unavailable, immediately `STOP` the current task.
8. Never route Steward repair through OpenRouter.

## Special case: model intelligence snapshot

For `getOpenRouterModels`:

1. Always pass `ref=runtime-results`.
2. On the first `404`, dispatch `operation=model_intelligence` with a unique `operation_id`.
3. Poll `getOperationStatus(operation_id)` until terminal.
4. Only after `status=success` and `result_ready=true`, retry `getOpenRouterModels` once.
5. If that refreshed read still fails, hand the fault to DeepSeek Steward `REPAIR`.

## Result-reading rule

Do not probe `metadata.json` or operation result files to determine whether a task has started or finished. Those files are detail/result records and may legitimately be absent while the task is queued or running.

Use `getOperationStatus` first. Read detailed result files only after the status object says they are ready.

## Safety invariants

- DeepSeek Steward uses only the official DeepSeek API.
- DeepSeek unavailability is a hard stop.
- No OpenRouter or other-provider fallback is allowed for Steward.
- Autonomous repairs may not modify `tests/`, `.git/`, `artifacts/`, or `runtime_results/` as source repair targets.
- Verification must pass before any autonomous repair is delivered.
- No force-push is allowed.
- No infinite repair/retry loop is allowed.
