# Control Plane v2, Budget, Cancellation, and DeepSeek Recovery Policy

## Authority

- Web GPT owns user intent, public evidence collection, and user-facing delivery.
- Deterministic repository code owns locking, idempotency, state transitions, cancellation, token ceilings, budget arithmetic, validation, and safety boundaries.
- DeepSeek Steward is the highest technical diagnosis and recovery authority.
- DeepSeek uses the official DeepSeek API only. Its unavailability is a hard stop.

## Primary one-step submission

Normal Web GPT submission is one structured comment to control Issue `#15` through `submitExpertTeamOperation`.

The comment body is compact JSON containing at least:

- `command: submit_operation`;
- one unique `operation_id`;
- `operation`;
- a short `task_label`;
- `plan_json` for `execute_team`.

That single synchronous comment is both the durable receipt and the controller trigger. Web GPT must not call the direct production dispatch after posting the same operation comment.

The independent `operation-controller.yml` then:

1. validates and normalizes the comment;
2. checks the existing per-operation ledger for idempotency;
3. writes `accepted`;
4. dispatches the production Worker once;
5. watches the Worker handoff for approximately 105 seconds;
6. automatically dispatches DeepSeek Top Supervisor with `startup_timeout` evidence when no Worker-owned state appears.

This startup recovery does not depend on Web GPT noticing stale status or remembering another repair call. Direct `dispatchExpertTeamOperation` remains only an advanced recovery/fallback interface.

## One paid task at a time

`test2` permits exactly one operation to pass the paid-execution gate at a time.

The authoritative lock is:

`runtime_results/control/single_task_lock.json`

Every production Run performs an atomic lock acquisition before dependency installation and model inference.

- If the lock is idle or stale, the operation acquires it and may continue.
- If another operation owns it, the new operation becomes `BUSY`.
- A `BUSY` operation performs no paid model call, does not enter a hidden queue, does not replace a pending task, and does not cancel the active task.
- A repeated dispatch with the same active `operation_id` is idempotent and performs no second paid execution.
- The active operation refreshes lock and status heartbeats during long execution.
- The lock is released in the final workflow boundary and by the cancellation workflow.
- A stale lock may be replaced only after its expiry threshold.

The production workflow does not use a shared GitHub concurrency group because GitHub's one-pending replacement behavior is not an acceptable task queue.

## Operation ledger

The authoritative state for one task is:

`runtime_results/operations/<operation_id>/state.json`

Each attempt is retained at:

`runtime_results/operations/<operation_id>/attempts/<run_id>.json`

The legacy `current_operation_status.json` is only a dashboard for the active operation. It is not the source of truth for a newly submitted operation.

States include:

- `accepted`
- `queued`
- `running`
- `repairing`
- `retrying`
- `BUSY`
- `cancel_requested`
- `cancelled`
- `success`
- `STOP`
- `failure`

Every state includes the operation ID, Run ID when available, attempt number, active step, heartbeat, receipt ID, result truth, and repair state.

## Durable receipt

The normal Issue comment returned by `submitExpertTeamOperation` is the durable receipt. Advanced direct production dispatch may omit `receipt_comment_id`; the Worker then creates a server-side receipt.

Missing `receipt_comment_id` must never terminate a user task with entry-point `422`.

A receipt proves acceptance, while the operation ledger proves execution state.

## Budget policy

Default budget for one logical task:

- total hard cap: USD 1.00;
- normal execution allowance: USD 0.70;
- recovery reserve: USD 0.30;
- default expert output ceiling: 2,200 tokens;
- default red-team output ceiling: 1,600 tokens;
- default judge output ceiling: 3,200 tokens;
- default model-call timeout: 240 seconds.

The budget covers the original operation and at most one controlled technical recovery. Web GPT may set another budget, subject to the repository operator maximum.

Before any paid expert call, deterministic code must:

1. enforce the authoritative Execution Plan JSON Schema;
2. read current model pricing, expanding from the compact snapshot to the official catalog when required;
3. calculate conservative input and maximum output costs for every expert, red team, and judge;
4. include the primary model's transient retry and every declared fallback retry in the worst case;
5. reserve the configured recovery percentage;
6. stop before inference when the worst case exceeds the available phase budget;
7. publish `cost_preflight.json`.

Prompt requests such as “keep the answer short” are not budget controls. API-level `max_tokens` is mandatory for every model call.

A 402, affordability error, or preflight budget failure must not be retried unchanged. It routes to the DeepSeek Top Supervisor for a lower-cost, lower-token, schema-valid plan that preserves user intent and never increases the user's budget.

A Top-Supervisor execution plan must fit entirely inside the reserved recovery budget. After an internal whole-operation retry or a prior Top-Supervisor recovery Run, the recovery budget is consumed and a second recovery dispatch is prohibited.

## Model-call resilience

Each model call has:

- an explicit output-token ceiling;
- a timeout;
- at most one same-model retry for transient timeout, 429, 502, 503, or malformed provider response;
- up to two plan-approved fallback models;
- an audit record identifying expert, role, model, attempt, token estimates, duration, and failure class.

402 and budget failures are not transient retries.

Parallel stages preserve successful member outputs. The stage's `failure_policy` and `minimum_successful_members` determine whether partial execution may continue.

## Cancellation

Web GPT may call `cancelExpertTeamOperation` with an `operation_id`.

The cancellation workflow:

1. records `cancel_requested`;
2. finds matching queued or running production Runs server-side;
3. calls normal cancellation;
4. force-cancels only when the Run remains active;
5. releases the single-task lock when owned by that operation;
6. records `cancelled` and publishes `cancellation_result.json`.

A user cancellation is not a technical fault and must not trigger DeepSeek repair.

## Internal DeepSeek diagnosis

Inside one paid operation:

1. the original operation runs once;
2. failure publishes `repairing`;
3. DeepSeek Steward diagnoses through the official API;
4. `NO_EDIT + READY` permits one unchanged retry only for a clearly transient provider failure and only when a clean pass fits the recovery reserve;
5. 402, budget, model-selection, or plan changes are escalated to the Top Supervisor rather than retried unchanged;
6. `EDIT` requires bounded repository changes and verification;
7. one retry is the maximum;
8. failure after that retry becomes `STOP` and is escalated.

## Top Supervisor

The independent `deepseek-supervisor.yml` workflow is outside the paid-task lock and uses a separate per-original-operation concurrency group.

It may:

- diagnose workflow, provider, integration, control-plane, budget, validation, publication, and startup faults;
- authorize a verified repository repair through a pull request;
- return `NO_EDIT + READY` with a replacement Execution Plan.

Any replacement plan must pass all of the following before redispatch:

1. the user task text is unchanged;
2. the replacement budget does not exceed the original budget;
3. runtime JSON Schema validation;
4. semantic plan validation;
5. current model-price recovery-budget preflight;
6. provenance injection identifying the Supervisor operation and original/effective plan hashes;
7. active and successful duplicate-Run checks;
8. proof that the one recovery budget has not already been consumed.

Raw matching Run count alone is not a recovery-budget ledger. The Supervisor uses published attempt and effective-plan evidence to determine whether recovery has already been used.

The Supervisor publishes its final result only after the bounded resume attempt has completed, been blocked, or failed.

## Repair delivery safety

Autonomous repair may not force-push or directly push to `main`.

- Verification must pass first.
- A repair branch is created.
- Delivery is through a pull request only.
- Workflow, policy, budget, dependency, Action-schema, and repair-control changes require human review and cannot auto-merge.
- If PR creation or merge is unavailable, the repair remains on the branch and the operation stops for review.

Tests, generated artifacts, runtime results, secrets, and `.git` data are protected repair targets.

## Result and audit truth

`result_ready=true` is allowed only when a local readable result exists and the runtime-result publication step succeeded.

Every operation publishes, when available:

- `metadata.json`
- `cost_preflight.json`
- `effective_execution_plan.json`
- `model_calls.json`
- `execution_trace.json`
- partial results
- repair or Supervisor evidence
- cancellation evidence
- `operation_audit.json`
- final result

`operation_audit.json` consolidates plan provenance, budget, calls, attempts, Runs, recovery, and result evidence. Missing evidence must be marked as missing; it must never be fabricated.

## Normal Web GPT flow

1. Generate a unique `operation_id`.
2. Post one `submitExpertTeamOperation` comment containing the complete structured command.
3. Do not call direct dispatch for the same operation.
4. Poll `getOperationState(operation_id)`.
5. On `BUSY`, report which operation owns the lock. The task was not queued and made no paid call.
6. On `running`, `repairing`, or `retrying`, continue tracking.
7. On `success` with `result_ready=true`, read the result, cost preflight, and audit.
8. On user request, call cancellation.
9. Startup timeout and production failure normally escalate automatically. Use manual `dispatchDeepSeekSupervisor` only for an Action-edge contradiction not already supervised.

## Hard boundaries

- One paid task at a time.
- Duplicate operation IDs are idempotent.
- No hidden task queue or pending-task replacement.
- One internal diagnosis and at most one recovery-budgeted retry.
- One Top-Supervisor recovery and at most one recovery-budgeted redispatch.
- No unchanged retry for 402 or budget failure.
- No direct autonomous push to `main`.
- No provider fallback for DeepSeek Steward.
- A total GitHub platform outage that prevents the Controller, production Worker, and Supervisor from starting cannot be repaired by repository code.
