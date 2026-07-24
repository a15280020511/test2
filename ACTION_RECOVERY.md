# Automatic DeepSeek Recovery and Permanent Operation Status Policy

## Purpose

`a15280020511/test2` uses DeepSeek Steward as the mandatory repository repair manager and one permanent current-status file as the normal Web GPT control plane.

The control plane and audit plane are separated:

- **Primary control plane**: `runtime_results/current_operation_status.json`.
- **Historical audit status**: `runtime_results/status/<operation_id>.json`.
- **Audit plane**: Run, Job, Step, logs, Artifact, metadata, repair evidence, and detailed runtime results.

Normal task flow must never use a workflow-run list or probe missing result files to discover task state.

## Permanent current-status control plane

`runtime_results/current_operation_status.json` is initialized on the `runtime-results` branch and is intended to remain permanently present.

After Web GPT receives HTTP `204` from `dispatchExpertTeamOperation`:

1. Poll `getCurrentOperationStatus` with `ref=runtime-results`.
2. HTTP `200` is the normal expected response at every point in the task lifecycle.
3. Compare the returned `operation_id` with the operation ID that was just dispatched.
4. If the IDs do not match, the new operation has not started yet. Treat this as pending/queued and keep polling. Do not treat it as a failure and do not dispatch a duplicate task.
5. If the IDs match and `status=running`, keep polling.
6. If the IDs match and `status=repairing`, DeepSeek Steward is repairing the original operation. Keep polling.
7. If the IDs match and `status=retrying`, the repaired operation is on its single allowed retry. Keep polling.
8. If the IDs match and `status=success` and `result_ready=true`, read the operation-specific result file.
9. If the IDs match and `status=STOP`, end the current task and report the preserved evidence. Do not substitute another provider for DeepSeek Steward.
10. If the IDs match and `status=failure`, use the supplied `run_id` for audit and diagnosis.

A `404` from `getCurrentOperationStatus` is no longer a normal queued state. It means the permanent control-plane file is missing or unreadable and is a repository/control-plane defect. Dispatch DeepSeek Steward `REPAIR`, then retry the permanent status endpoint until the repair workflow recreates it or DeepSeek hard-stops.

The permanent status file contains only small control fields: `operation_id`, `operation`, `status`, `run_id`, `result_ready`, `result_published`, `repair_status`, and `updated_at`.

## Result publication truth rule

`result_ready=true` is allowed only when both conditions are true:

1. the operation produced a local readable result; and
2. the workflow step that publishes the GPT-readable runtime result completed successfully.

A local file alone must never mark the remote result as ready. This prevents Web GPT from being told to read a result that was never successfully published to `runtime-results`.

## GitHub-internal automatic recovery

For `model_intelligence` and `execute_team`:

1. Run the requested operation normally.
2. If it succeeds, finish normally.
3. If it fails, automatically create an evidence-based Support Packet from the operation, error output, and available context.
4. Publish `status=repairing` to the permanent control plane on a best-effort basis.
5. Automatically invoke `DeepSeek Steward` in `REPAIR` mode through the official DeepSeek API.
6. If DeepSeek is unreachable, authentication fails, official model discovery fails, or DeepSeek inference fails, immediately `STOP` the current operation. Never use OpenRouter or another provider as a Steward fallback.
7. If Steward returns `EDIT`, apply only bounded repository edits and run the mandatory verification gate.
8. Before the single retry, publish `status=retrying` on a best-effort basis.
9. Retry the original operation exactly once in the repaired workspace.
10. Deliver the verified repair only when verification and the single retry both succeed.
11. If the retry fails, `STOP`; do not start an infinite repair loop.
12. If Steward returns `NO_EDIT` or `STOP`, end the operation and preserve the evidence.

The automatic repair cycle is limited to **one repair attempt and one retry per original operation**.

## Web GPT Action-edge recovery

A workflow cannot directly observe an HTTP/client failure that occurs before a workflow is dispatched. Web GPT must distinguish a real technical failure from a normal operation-ID mismatch while a newly accepted task has not started.

Automatic DeepSeek REPAIR triggers include:

- `getCurrentOperationStatus` returns `404` or an unparseable response;
- a required Action call returns an unexpected GitHub REST `4xx` or `5xx`;
- Execution Plan Schema cannot be read or parsed;
- current status matches the requested operation, reports `status=success` and `result_ready=true`, but the expected result still cannot be read;
- the same required read still fails after its documented refresh step.

Mandatory protocol:

1. Do not improvise a repository code fix in Web GPT.
2. Build a Support Packet from the failed call and available evidence.
3. Dispatch `operation=deepseek_steward` with `steward_mode=REPAIR`.
4. Track the repair through `getCurrentOperationStatus`.
5. Ignore an old/mismatched `operation_id` while the repair workflow is queued; keep polling the permanent status file.
6. Read the Steward result only after the permanent current status matches the repair operation ID and reaches a terminal state.
7. Resume the original task only when the Steward result says `READY`.
8. If the official DeepSeek API is unavailable, immediately `STOP` the current task.
9. Never route Steward repair through OpenRouter.

DeepSeek unavailability is a hard stop.

## Special case: model intelligence snapshot

For `getOpenRouterModels`:

1. Always pass `ref=runtime-results`.
2. On the first `404`, dispatch `operation=model_intelligence` with a unique operation ID.
3. Poll `getCurrentOperationStatus`.
4. While the permanent status contains a different operation ID, treat the refresh as pending/queued and keep polling.
5. Only after the status matches the refresh operation ID and reports `status=success` and `result_ready=true`, retry `getOpenRouterModels` once.
6. If the refreshed read still fails, hand the fault to DeepSeek Steward `REPAIR`.

## Audit rule

Run, Job, Step, logs, Artifact, per-operation status, metadata, and repair evidence are audit records, not the primary control plane.

Use the `run_id` from the matching permanent current status for optional Run/Job/Step/Artifact audit reads. Do not list workflow runs to locate a task.

## Safety invariants

- DeepSeek Steward uses only the official DeepSeek API.
- DeepSeek unavailability is a hard stop.
- Never route Steward repair through OpenRouter.
- No OpenRouter or other-provider fallback is allowed for Steward.
- Autonomous repairs may not modify `tests/`, `.git/`, `artifacts/`, or `runtime_results/` as source repair targets.
- Verification must pass before any autonomous repair is delivered.
- No force-push is allowed.
- No infinite repair/retry loop is allowed.
- One repository production task runs at a time under the existing workflow concurrency group.
