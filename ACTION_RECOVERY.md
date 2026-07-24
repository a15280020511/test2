# Automatic DeepSeek Recovery, Durable Receipt, and Top-Supervisor Policy

## Purpose

`a15280020511/test2` uses DeepSeek Steward as the highest technical control layer.

The system separates:

- **Durable receipt plane**: one GitHub issue comment per production operation in control issue `#15`.
- **Primary control plane**: `runtime_results/current_operation_status.json`.
- **Historical audit status**: `runtime_results/status/<operation_id>.json`.
- **Audit plane**: Run, Job, Step, logs, Artifact, metadata, repair evidence, and detailed runtime results.
- **Top technical supervisor**: `.github/workflows/deepseek-supervisor.yml`, with an independent concurrency group.

Web GPT owns user intent. DeepSeek Steward owns technical diagnosis and recovery.

## Durable operation receipt

For every production operation:

1. Generate one unique `operation_id`.
2. Web GPT should normally call `createOperationReceipt` first and keep the returned comment `id` as `receipt_comment_id`.
3. The receipt contains at least `operation_id`, `operation`, and a short task label. Never include secrets.
4. Dispatch the production workflow with the same `operation_id`.
5. When Web GPT supplies `receipt_comment_id`, the workflow uses it.
6. When Web GPT omits `receipt_comment_id`, the production workflow must automatically create the receipt server-side in issue `#15` before publishing running status.
7. Missing `receipt_comment_id` must never produce an entry-point `422` that ends the user task.
8. If server-side receipt creation fails, the production job fails and automatically escalates to DeepSeek Top Supervisor.

The receipt is durable evidence that the operation was accepted. Pre-dispatch receipt creation is preferred because it is synchronous, but server-side fallback is mandatory so correctness does not depend on Web GPT remembering an Action call order.

## Operation-to-Run correlation

Every production workflow Run uses the GitHub `run-name`:

`expert-<operation_id>-<operation>`

The DeepSeek supervisor uses:

`supervisor-<supervisor_operation_id>-for-<original_operation_id>`

This allows server-side correlation without exposing workflow-run lists to Web GPT.

## Normal production control flow

After the production dispatch returns HTTP `204`:

1. Poll `getCurrentOperationStatus` with `ref=runtime-results`.
2. HTTP `200` is the normal expected control response.
3. If returned `operation_id` matches the submitted ID and `status=running`, keep polling.
4. If it matches and `status=repairing`, DeepSeek is repairing an internal operation failure. Keep polling.
5. If it matches and `status=retrying`, the repaired operation is on its single internal retry. Keep polling.
6. If it matches and `status=success` and `result_ready=true`, read the operation-specific result.
7. If it matches and `status=STOP`, read available evidence and stop. Never substitute another provider for DeepSeek.
8. If it matches and `status=failure`, route the technical failure to the top supervisor unless a top-supervisor recovery is already active for this original operation.

Do not use workflow-run lists or missing result files as the normal task-state mechanism.

## Startup timeout rule

A production dispatch may return HTTP `204` before the new Run has updated the permanent current status.

The old behavior of waiting indefinitely on `idle` or an older `operation_id` is forbidden.

After a successful production dispatch:

1. If `getCurrentOperationStatus` is `idle` or contains a different `operation_id`, treat the first read as pending.
2. Repeat the control read once after a short wait.
3. If the second consecutive read still does not show the submitted `operation_id`, or roughly 90 seconds have elapsed, automatically dispatch the independent DeepSeek Top Supervisor.
4. Do not end the user task at this point.
5. Do not blindly dispatch the original production task again.
6. The supervisor must inspect GitHub server-side for a Run whose `display_title` contains the original `operation_id`.
7. If a matching Run is active, do not duplicate it; report that it is already active and continue tracking.
8. If there is no matching Run, DeepSeek diagnoses the startup/dispatch/control problem and may resume the original dispatch once when safe.
9. If there is one known failed matching Run, DeepSeek may repair and resume it once.
10. If two or more matching Runs already exist, automatic redispatch is blocked.

## Highest-level DeepSeek supervisor

All technical problems ultimately route to `.github/workflows/deepseek-supervisor.yml`.

The supervisor:

1. Uses a concurrency group separate from `expert-team-production`.
2. Uses only the official DeepSeek API.
3. Receives the original `operation_id`, durable receipt ID when available, failure class, Support Packet, and a bounded original dispatch payload.
4. Runs DeepSeek Steward in `REPAIR` mode.
5. Applies only bounded repository edits authorized by Steward.
6. Runs mandatory verification before repair delivery.
7. Publishes its own status and result through the same permanent control plane.
8. Checks matching production Runs server-side before any resume.
9. Redispatches the original production operation at most once when Steward says `READY` and no active/successful/duplicate Run blocks it.
10. Never recursively launches another top supervisor for its own failure.

## Automatic escalation from production Workflow

The production workflow must automatically dispatch the independent top supervisor when its main job fails outside the normal successful path.

This includes failures in:

- checkout;
- Python setup;
- automatic receipt creation;
- status publication;
- dependency installation;
- repository-context preparation;
- `managed_operation` after its own internal repair attempt;
- repair delivery;
- Artifact upload;
- runtime-result publication;
- final status publication;
- any other production job step.

The escalation job runs separately from the failed production job and passes the original dispatch payload plus the known failed `GITHUB_RUN_ID` to the top supervisor.

When the supervisor later checks matching Runs, the known failed Run ID is not treated as an active Run merely because the parent workflow is still completing its escalation job.

## GitHub-internal automatic recovery

Inside a running `model_intelligence` or `execute_team` operation:

1. Run normally.
2. If it succeeds, finish normally.
3. If it fails, publish `status=repairing` on a best-effort basis.
4. Automatically invoke DeepSeek Steward REPAIR through the official DeepSeek API.
5. If DeepSeek is unavailable, STOP. Never use OpenRouter or another provider as fallback.
6. If Steward returns an EDIT, apply bounded edits and run verification.
7. Publish `status=retrying` before the one allowed retry.
8. Retry the original operation exactly once.
9. Deliver the verified repair only when verification and retry succeed.
10. If the retry fails, STOP and preserve evidence.

This internal repair loop is subordinate to the top supervisor. If the production workflow itself still fails, the independent top supervisor receives the failure.

## Action-edge technical failures

Web GPT must route any technical anomaly to the top supervisor, including:

- `getCurrentOperationStatus` returns `404` or unparseable data;
- pre-dispatch receipt creation fails;
- production dispatch returns unexpected `4xx/5xx` for any reason other than a stale Builder schema that can be corrected immediately;
- startup remains `idle` or mismatched for two consecutive control reads or about 90 seconds;
- Execution Plan Schema cannot be read or parsed;
- model-intelligence refresh fails after its documented refresh sequence;
- current status says `success` and `result_ready=true` but the expected result cannot be read;
- an audit endpoint reveals a technical contradiction;
- any technical condition prevents a trustworthy terminal result.

Mandatory behavior:

1. Preserve the original user task and original production dispatch payload.
2. Do not improvise repository code repair in Web GPT.
3. Build an evidence-based Support Packet.
4. Dispatch `dispatchDeepSeekSupervisor`.
5. Track the supervisor through `getCurrentOperationStatus`.
6. While current status still belongs to the original or an older operation, keep polling until the supervisor operation appears.
7. Read `getDeepSeekStewardResult` only after the current status matches the supervisor operation and reaches a terminal state.
8. Resume the original user task only when Steward says `READY`, or when the supervisor confirms an already-active matching production Run.
9. If official DeepSeek is unavailable, STOP. Never route repair through OpenRouter.

## Model-intelligence special case

For `getOpenRouterModels`:

1. Always pass `ref=runtime-results`.
2. On first `404`, create a durable receipt for a unique `model_intelligence` operation when possible.
3. Dispatch `model_intelligence`; the workflow auto-creates a receipt when none was supplied.
4. Track via `getCurrentOperationStatus`.
5. Apply the same two-read/90-second startup timeout rule.
6. Only after matching `status=success` and `result_ready=true`, retry `getOpenRouterModels` once.
7. If the refreshed read still fails, dispatch the top supervisor.

## Result publication truth rule

`result_ready=true` is allowed only when:

1. the operation produced a local readable result; and
2. the workflow step that publishes the GPT-readable runtime result completed successfully.

A local result alone must never mark the remote result ready.

## Audit rule

Run, Job, Step, logs, Artifact, historical per-operation status, metadata, receipt comments, and repair evidence are audit records.

The primary Web GPT control path is:

`receipt when available -> production dispatch -> server-side receipt fallback -> permanent current status -> result`

On technical anomaly it becomes:

`available receipt evidence -> DeepSeek Top Supervisor -> verified repair/diagnosis -> bounded resume -> permanent current status -> result`

Do not expose or use workflow-run lists as the normal Web GPT control mechanism.

## Safety invariants

- DeepSeek Steward is the highest technical authority inside `test2`.
- DeepSeek Steward uses only the official DeepSeek API.
- DeepSeek unavailability is a hard stop.
- Never route Steward repair through OpenRouter.
- No OpenRouter or other-provider fallback is allowed for Steward.
- Autonomous repairs may not modify `tests/`, `.git/`, `artifacts/`, or `runtime_results/` as source repair targets.
- Verification must pass before any autonomous repair is delivered.
- No force push is allowed.
- No infinite repair, supervisor, or redispatch loop is allowed.
- One internal repair cycle and one internal retry remain the maximum inside one production operation.
- One top-supervisor recovery and one bounded production redispatch are the maximum after one failed or missing production attempt.
- A total GitHub Actions/API platform outage that prevents both production and supervisor workflows from starting cannot be repaired by repository code; preserve available receipt evidence and report a platform hard stop.
