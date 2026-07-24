# Independent DeepSeek Control Plane

This repository is the independent control plane for `a15280020511/test`.

## Authority and boundaries

Web GPT must contact this control plane before any production execution in `test`.
The work repository is an execution laboratory, not an independent task entry.

The control plane owns:

- task identity and revision control;
- duplicate-run prevention;
- signed execution authorization;
- start, continuous monitoring, status, cancel, force-cancel, and restart;
- independent retrieval of Run, Job, Step, Log, and Artifact evidence;
- automatic DeepSeek final review after successful execution;
- automatic DeepSeek diagnosis after failed execution;
- controlled cross-repository repair branch and PR creation;
- emergency rescue when the normal control or work workflow is unhealthy.

The work repository owns deterministic execution, simulation, calculation, logs,
and result artifacts. It must reject unsigned or expired control tickets.

## Workflows

- `.github/workflows/deepseek-control.yml` is the normal mandatory entry. `START`
  and `RESTART` remain active until the target Run reaches a terminal state, then
  perform review or diagnosis automatically.
- `.github/workflows/deepseek-rescue.yml` is an isolated emergency entry. It has no
  checkout and does not depend on scripts from either repository.

## Required secrets

Configure these Actions secrets in `test2`:

1. `DEEPSEEK_API_KEY`
   - Official DeepSeek API key.
   - Used for final review, diagnosis, and repair planning.

2. `CONTROL_PLANE_TOKEN`
   - Fine-grained PAT or GitHub App installation token.
   - Restrict repository access to `a15280020511/test`.
   - Required permissions:
     - Actions: read and write;
     - Contents: read and write;
     - Pull requests: read and write.
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
→ control plane polls the exact Run
→ deterministic execution and Artifact
→ automatic DeepSeek REVIEW when successful
   or automatic DeepSeek DIAGNOSE when failed
→ APPROVE / REPLAN / COLLECT / REPAIR / STOP
→ Web GPT publication only after APPROVE
```

## Duplicate protection

Identity is `task_id + revision`.

- An active matching Run is monitored instead of being dispatched again.
- A successful matching Run is reviewed instead of being duplicated by `START`.
- `RESTART` may force-cancel an old Run and dispatch an explicitly supplied revision.
- The work workflow also uses a task-level concurrency group as a second barrier.

## Cancellation

- `CANCEL` requests normal GitHub cancellation.
- `FORCE_CANCEL` invokes GitHub force-cancel.
- A target Run that exceeds the three-hour monitor ceiling is force-cancelled.
- `deepseek-rescue.yml` provides cancellation and diagnosis when the normal control
  workflow is unhealthy.

## Repair

`REPAIR` gathers the failed Run, Jobs, Log tails, Artifacts, and a bounded current
repository context. DeepSeek may return minimal complete-file edits. The controller:

1. validates every edit path against a restricted code surface;
2. creates a dedicated repair branch;
3. applies the proposed edits using the GitHub Contents API;
4. creates a PR against `main`;
5. deliberately does not auto-merge.

Repository Validation and Think Tank self-test must pass before a repair PR is
merged. DeepSeek has the highest technical decision priority, while deterministic
CI remains the write-safety gate.

## Final review rule

A GitHub `success` conclusion means only that execution completed without a terminal
runner error. It is not publication approval. `START`, `RESTART`, and explicit
`REVIEW` publish only when the DeepSeek result says `APPROVE`.

## Physical limits

This design survives failure of the `test` workflow, scripts, dependencies, and
result-publication path because the control and rescue workflows live in `test2`.
It cannot operate when GitHub Actions itself is unavailable, the control token is
revoked, or the official DeepSeek API is unreachable. Those are external physical
boundaries and must result in a truthful stop.
