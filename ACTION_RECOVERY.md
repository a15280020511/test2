# Automatic DeepSeek Recovery, Durable Receipt, and Top-Supervisor Policy

## Purpose

`a15280020511/test2` uses DeepSeek Steward as the **highest technical control layer**.

The system separates:

- **Durable receipt plane**: GitHub issue comments in control issue `#15`;
- **Primary control plane**: `runtime_results/current_operation_status.json`;
- **Audit plane**: Run, Job, Step, logs, Artifact, metadata, budget evidence and repair evidence;
- **Independent supervisor**: `.github/workflows/deepseek-supervisor.yml`;
- **Temporary plugin plane**: task-scoped tool environments that are destroyed after use.

Web GPT owns user intent and all user budget communication. DeepSeek owns technical diagnosis, result review and recovery decisions. GitHub owns deterministic enforcement.

## Durable operation receipt

For every production operation:

1. Generate one unique `operation_id`.
2. Web GPT should normally call `createOperationReceipt` first and keep the returned comment ID as `receipt_comment_id`.
3. Never include secrets.
4. Dispatch the production workflow with the same `operation_id`.
5. The workflow uses a supplied receipt when present.
6. For non-paid operations, if Web GPT omits the receipt, server-side fallback is mandatory.
7. Missing `receipt_comment_id` for a non-paid operation must never produce an entry-point `422` that ends the user task.
8. If server-side receipt creation fails, the production job fails and escalates to DeepSeek Top Supervisor.

### Paid expert-team receipt

`execute_team` is different. It must receive a durable JSON receipt created by Web GPT **after** the user selects a budget. No server-side fallback may invent user approval.

The receipt must exactly match the submitted plan:

- execution `operation_id`;
- DeepSeek ASSIST `operation_id`;
- budget tier;
- estimated cost range;
- maximum cost;
- maximum model calls;
- maximum output tokens per call;
- truthful approval reference.

Any mismatch stops before paid inference.

## Operation-to-Run correlation

Production Run name:

```text
expert-<operation_id>-<operation>
```

Supervisor Run name:

```text
supervisor-<supervisor_operation_id>-for-<original_operation_id>
```

This correlation supports server-side duplicate detection without making Run lists the normal Web GPT control path.

## Normal control flow

After dispatch returns HTTP `204`:

1. Poll `getCurrentOperationStatus` with `ref=runtime-results`.
2. If the returned `operation_id` matches and status is `running`, continue polling.
3. `repairing` and `retrying` are allowed only for operations eligible for non-paid automatic recovery.
4. If status is `success` and `result_ready=true`, read the operation result.
5. If status is `STOP` or `failure`, preserve evidence and route technical diagnosis to DeepSeek.
6. Do not treat a missing result file or old status as proof that no Run exists.

## Startup timeout rule

After a successful dispatch:

1. If current status is `idle` or belongs to another operation, treat the first read as pending.
2. Perform a second consecutive read after a short wait.
3. If the second consecutive read is still mismatched, or roughly 90 seconds have elapsed, dispatch the independent supervisor.
4. Do not blindly dispatch the original task again.
5. The supervisor checks GitHub server-side for matching Runs.
6. An active matching Run blocks duplication.
7. Two or more matching Runs block another automatic dispatch.
8. A missing or known failed non-paid operation may receive one bounded recovery.
9. Any missing, failed or possibly-started `execute_team` operation is blocked from automatic supervisor redispatch because the system cannot prove zero spend. Web GPT must obtain a new user-approved budget receipt and use a new `operation_id` before another paid attempt.

## Highest-level DeepSeek supervisor

All technical problems ultimately route to `.github/workflows/deepseek-supervisor.yml`.

The supervisor:

1. runs in a separate concurrency group;
2. uses only the official DeepSeek API;
3. receives the original operation, receipt, Run evidence and dispatch payload;
4. diagnoses repository, plugin, provider, Action-edge and publication failures;
5. may create a bounded repository repair;
6. runs verification before repair delivery;
7. checks duplicate Runs before any resume;
8. permits at most one **non-paid production redispatch**;
9. never recursively launches another supervisor for its own failure;
10. never fabricates or changes user budget approval.

## Paid expert-team retry prohibition

A failed `execute_team` attempt may already have consumed model calls even when GitHub reports failure or no final result.

Therefore:

1. `managed_operation` must not internally retry `execute_team`.
2. The top supervisor may diagnose the failure and repair repository code.
3. The top supervisor must not redispatch the old paid operation.
4. It must not replace models, reduce the plan or reuse the old receipt to create more calls.
5. The current operation terminates with `STOP` and `budget_reapproval_required=true`.
6. Web GPT must explain the failed attempt and possible prior spend to the user.
7. DeepSeek ASSIST must produce a new budget proposal where needed.
8. The user must explicitly approve the new amount.
9. Web GPT must create a new durable budget receipt.
10. A new `operation_id` must be used for the new attempt.

This rule is deterministic and cannot be overridden by DeepSeek output.

## Non-paid internal automatic recovery

Only non-paid operational work, currently `model_intelligence`, may use the internal repair loop:

1. run once;
2. on failure, publish `repairing`;
3. call DeepSeek REPAIR through the official API;
4. if DeepSeek is unavailable, STOP;
5. when DeepSeek returns an evidenced EDIT, apply the smallest repair;
6. run verification;
7. publish `retrying`;
8. retry exactly once;
9. preserve evidence whether the retry succeeds or fails.

The phrase “one repair cycle and one retry” applies only to this non-paid path.

## Automatic escalation from production Workflow

The production workflow escalates failures outside a successful path, including:

- checkout and Python setup;
- receipt creation or receipt verification;
- plugin installation or cleanup;
- OpenRouter/Agent Framework integration;
- official DeepSeek integration;
- status and result publication;
- Artifact upload;
- any other step preventing a trustworthy terminal result.

Escalation always permits diagnosis. It permits automatic redispatch only when the deterministic paid/non-paid rules allow it.

## Action-edge failures

Web GPT routes technical anomalies to the supervisor when:

- current status is `404`, invalid or contradictory;
- startup remains mismatched after the bounded check;
- the Action dispatch returns unexpected errors;
- the Execution Plan or budget receipt cannot be verified;
- result publication contradicts Run evidence;
- a plugin fails to install, execute or clean up;
- any condition prevents a trustworthy terminal result.

Web GPT must not improvise code repair. It builds an evidence-based Support Packet and follows the DeepSeek result, subject to deterministic budget gates.

## Model-intelligence special case

`model_intelligence` is non-paid control support and may use one bounded repair/retry:

1. read the existing runtime snapshot;
2. on missing snapshot, dispatch a unique refresh operation;
3. follow the same two-read/90-second startup rule;
4. use one bounded non-paid recovery when technically justified;
5. publish the refreshed snapshot only after the matching operation succeeds.

## Result truth rule

`result_ready=true` requires both:

1. a local readable result; and
2. successful publication of the GPT-readable result.

A local file, successful process exit or successful GitHub Run alone does not prove analytical quality. DeepSeek REVIEW remains the final publication gate for expert-team results.

## Audit rule

Preserve:

- durable receipts;
- DeepSeek ASSIST and REVIEW results;
- budget verification;
- plugin lifecycle evidence;
- Run, Job, Step and log evidence;
- Artifacts and repair evidence.

Normal flow:

```text
receipt -> dispatch -> permanent status -> result -> DeepSeek REVIEW
```

Technical anomaly:

```text
evidence -> DeepSeek Top Supervisor -> diagnosis/repair ->
non-paid bounded recovery OR paid STOP and new user approval
```

## Safety invariants

- DeepSeek Steward is the highest technical authority inside `test2`.
- DeepSeek uses only the official DeepSeek API.
- DeepSeek unavailability is a hard stop.
- Never route Steward through OpenRouter.
- Never modify or fabricate user budget approval.
- Never automatically retry or redispatch a failed paid expert-team operation.
- Autonomous repairs may not modify `tests/`, `.git/`, `artifacts/`, `runtime_results/` or Secrets.
- Verification must pass before repair delivery.
- No force push and no infinite recovery loop.
- One internal repair cycle and one internal retry are the maximum for a non-paid operation.
- One top-supervisor recovery and one bounded non-paid production redispatch are the maximum after a failed or missing non-paid attempt.
- A GitHub Actions/API outage that prevents both production and supervisor workflows from starting is outside repository repair capability; preserve evidence and report `STOP`.
