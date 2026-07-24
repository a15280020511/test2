# Automatic DeepSeek Recovery Policy

## Purpose

`a15280020511/test2` uses DeepSeek Steward as the mandatory repository repair manager.

The system has two recovery layers:

1. **GitHub-internal automatic recovery** for failures inside `model_intelligence` and `execute_team`.
2. **Web GPT Action-edge automatic handoff** for failures that happen before a GitHub workflow can observe them, such as GitHub REST 4xx/5xx responses, missing runtime-result files, or an unparseable Action response.

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

## Web GPT Action-edge automatic handoff

A repository workflow cannot directly observe an HTTP error that occurs in the GPT Action client before a workflow is dispatched. Therefore Web GPT must treat the following as an automatic repair trigger rather than ending the task immediately:

- GitHub REST `4xx` or `5xx` from a test2 Action operation;
- required Action response is empty or cannot be parsed;
- Execution Plan Schema cannot be read or parsed;
- runtime result or operation metadata is unexpectedly missing after the corresponding Run completed successfully;
- the same required read operation still fails after its documented refresh step.

Mandatory protocol:

1. Do not improvise a repository code fix in Web GPT.
2. Build a Support Packet from the failed call and all available evidence.
3. Automatically dispatch `operation=deepseek_steward` with `steward_mode=REPAIR`.
4. Wait for the Steward Run to finish and read the Steward result.
5. If the Steward result is `READY`, retry the originally failed Action step once and continue the original user task.
6. If the Steward operation itself fails because DeepSeek official API is unavailable, immediately `STOP` the current user task.
7. Never route Steward repair through OpenRouter.

### Special case: model intelligence snapshot

For `getOpenRouterModels`:

1. Always pass `ref=runtime-results`.
2. On the first `404`, dispatch `operation=model_intelligence` and wait for success.
3. Retry `getOpenRouterModels` once with `ref=runtime-results`.
4. If the second read still fails, automatically hand the fault to DeepSeek Steward `REPAIR`.

## Result-reading rule

Runtime-result reads must explicitly use `ref=runtime-results`. The Action schema marks this parameter as required so GPT clients cannot silently omit it and accidentally read from `main`.

## Safety invariants

- DeepSeek Steward uses only the official DeepSeek API.
- DeepSeek unavailability is a hard stop.
- No OpenRouter or other-provider fallback is allowed for Steward.
- Autonomous repairs may not modify `tests/`, `.git/`, `artifacts/`, or `runtime_results/` as source repair targets.
- Verification must pass before any autonomous repair is delivered.
- No force-push is allowed.
- No infinite repair/retry loop is allowed.
