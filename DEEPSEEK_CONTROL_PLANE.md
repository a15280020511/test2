# Independent DeepSeek Control Plane

This repository is the independent control plane for `a15280020511/test`.

## Authority and boundaries

Web GPT must contact this control plane before any production execution in `test`.
The work repository is an execution laboratory, not an independent task entry.

The control plane owns:

- task identity and revision control;
- duplicate-run prevention;
- signed execution authorization;
- start, status, cancel, force-cancel, restart, review, and diagnosis;
- independent retrieval of Run, Job, Step, Log, and Artifact evidence;
- final DeepSeek quality review before publication;
- emergency rescue when the normal work workflow is unhealthy.

The work repository owns deterministic execution, simulation, calculation, logs,
and result artifacts. It must reject unsigned or expired control tickets.

## Workflows

- `.github/workflows/deepseek-control.yml` is the normal mandatory entry.
- `.github/workflows/deepseek-rescue.yml` is an isolated emergency entry. It has no
  checkout and does not depend on scripts from either repository.

## Required secrets

Configure these Actions secrets in `test2`:

1. `DEEPSEEK_API_KEY`
   - Official DeepSeek API key.
   - Used for review and diagnosis.

2. `CONTROL_PLANE_TOKEN`
   - Fine-grained PAT or GitHub App installation token.
   - Restrict repository access to `a15280020511/test`.
   - Required permissions:
     - Actions: read and write;
     - Contents: read.
   - Do not grant repository deletion, administration, secrets, members, or billing.

3. `CONTROL_TICKET_SECRET`
   - Random high-entropy shared secret, at least 32 bytes.
   - Store the same value as an Actions secret in both `test2` and `test`.
   - It signs one-hour execution tickets. Never place it in repository files, logs,
     summaries, issues, or artifacts.

The connector cannot create or read GitHub Actions secrets. They must be entered
manually in repository settings before the new production path is enabled.

## Task lifecycle

```text
Web GPT
→ DeepSeek Control START
→ duplicate check
→ signed ticket
→ test/think-tank.yml
→ deterministic execution
→ Artifact
→ DeepSeek Control REVIEW
→ APPROVE / REPLAN / COLLECT / REPAIR / STOP
→ Web GPT publication only after APPROVE
```

## Duplicate protection

Identity is `task_id + revision`.

- An active matching Run returns `DUPLICATE_ACTIVE` and is not dispatched again.
- A successful matching Run returns `DUPLICATE_COMPLETED` for `START`.
- `RESTART` may force-cancel an old Run and dispatch an explicitly supplied revision.
- The work workflow also uses a task-level concurrency group as a second barrier.

## Cancellation

- `CANCEL` requests normal GitHub cancellation.
- `FORCE_CANCEL` invokes GitHub force-cancel.
- `deepseek-rescue.yml` provides the same controls when the normal control workflow is unhealthy.

## Final review rule

A GitHub `success` conclusion means only that execution completed without a terminal
runner error. It is not publication approval. Web GPT must dispatch `REVIEW` and
publish only when the DeepSeek result says `APPROVE`.

## Physical limits

This design survives failure of the `test` workflow, scripts, dependencies, and
result-publication path because the control and rescue workflows live in `test2`.
It cannot operate when GitHub Actions itself is unavailable, the control token is
revoked, or the official DeepSeek API is unreachable. Those are external physical
boundaries and must result in a truthful stop.
