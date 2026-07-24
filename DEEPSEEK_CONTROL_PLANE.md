# Independent DeepSeek Control Plane

This repository is the independent control plane for `a15280020511/test`.

## Authority and boundaries

Web GPT must contact this control plane before any production execution in `test`. The work repository is an execution laboratory, not an independent task entry.

DeepSeek has the highest AI technical decision priority. Minimal deterministic code may validate explicit inputs, authorization, and credentials, but no production dispatch may occur before the mandatory DeepSeek entry decision.

The control plane owns:

- mandatory DeepSeek entry review before `START` or `RESTART`;
- evidence sufficiency, assumption, risk, and execution-plan guidance;
- task identity and revision control;
- duplicate-run prevention;
- signed execution authorization;
- start, continuous monitoring, status, cancel, force-cancel, and restart;
- independent retrieval of Run, Job, Step, Log, and Artifact evidence;
- automatic DeepSeek final review after successful execution;
- automatic DeepSeek diagnosis after failed execution;
- automatic restricted repair PR creation when diagnosis confirms a target-repository defect;
- automatic checkout-free diagnosis and bounded self-repair escalation when the control workflow itself fails;
- emergency rescue when the normal control or work workflow is unhealthy.

The work repository owns deterministic execution, simulation, calculation, logs, provenance, and result Artifacts. It must reject unsigned, mismatched, or expired control tickets.

## Workflows

- `.github/workflows/deepseek-control.yml` is the only normal mandatory entry. Its production command is `scripts.deepseek_priority_control`, which runs DeepSeek before dispatch and then delegates mechanical monitoring to `scripts.cross_repo_control`.
- `.github/workflows/deepseek-rescue.yml` is an isolated manual emergency entry. It has no checkout and does not depend on scripts from either repository.
- `.github/workflows/deepseek-control-failure-sentinel.yml` independently diagnoses a failed control-plane workflow without checkout. It may request one failed-job rerun or dispatch `deepseek-supervisor.yml` for a confirmed persistent control-repository defect.
- `.github/workflows/deepseek-supervisor.yml` performs the highest-level bounded repair planning and verified delivery. The sentinel passes `retry_dispatch_json={}` so control-plane self-repair never automatically restarts paid expert work.

## Required secrets

Configure these Actions secrets in `test2`:

1. `DEEPSEEK_API_KEY`
   - Official DeepSeek API key.
   - Used for entry gating, final review, diagnosis, and repair planning.

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
   - It signs one-hour execution tickets and must never appear in repository files, logs, summaries, issues, or Artifacts.

The connector cannot create or read GitHub Actions secrets. They must be entered manually in repository settings.

## Task lifecycle

```text
Web GPT
→ DeepSeek Control START
→ mandatory DeepSeek entry gate
   → READY: use the supplied or corrected complete execution plan
   → COLLECT / REPLAN / REPAIR / STOP: block dispatch
→ duplicate check
→ signed ticket
→ test/think-tank.yml
→ control plane polls the exact Run
→ deterministic execution and Artifact
→ automatic DeepSeek REVIEW when successful
   or automatic DeepSeek DIAGNOSE when failed
→ if target-repository defect: automatic restricted REPAIR PR
→ APPROVE / COLLECT / REPLAN / REPAIR / STOP
→ Web GPT publication only after APPROVE
```

If `deepseek-control.yml` itself fails:

```text
failed control Run
→ checkout-free control failure sentinel
→ exact Run / Jobs / Log tails
→ strongest DeepSeek diagnosis
→ RETRY: one failed-job rerun when run_attempt < 2
→ REPAIR + CONTROL_REPOSITORY: dispatch highest DeepSeek Supervisor
→ verified repair delivery and CI-gated PR
→ WAIT / STOP: retain diagnosis without unsafe action
```

## Entry gate

`START` and `RESTART` parse the task, evidence, candidate plan, and support context, then call the strongest available official DeepSeek model before any work-repository dispatch.

- `READY` requires a complete effective plan with non-empty `steps[]`.
- A DeepSeek replacement plan overrides the supplied candidate for that dispatch.
- `COLLECT`, `REPLAN`, `REPAIR`, and `STOP` produce `ENTRY_BLOCKED`; no ticket is issued and no work Run starts.
- The entry result is preserved in `entry-gate.json` and the control Artifact.

## Duplicate protection

Identity is `task_id + revision`.

- An active matching Run is monitored instead of being dispatched again.
- A successful matching Run is reviewed instead of being duplicated by `START`.
- `RESTART` may force-cancel an old Run and dispatch an explicitly supplied revision.
- The work workflow uses a task-level concurrency group as a second barrier.

## Cancellation and monitoring

- `CANCEL` requests normal GitHub cancellation.
- `FORCE_CANCEL` invokes GitHub force-cancel.
- A target Run that exceeds the three-hour monitor ceiling is force-cancelled.
- `deepseek-rescue.yml` provides cancellation and diagnosis when the normal controller is unhealthy.

## Repair

For a failed target Run, the controller collects the exact Run, Jobs, Log tails, Artifacts, and bounded current repository context.

When DeepSeek diagnosis returns `REPAIR`, the priority controller automatically performs a dedicated repair call. The repair controller:

1. validates every edit path against a restricted code surface;
2. creates a dedicated repair branch;
3. applies full-file edits through the GitHub Contents API;
4. creates a PR against `main`;
5. redacts the short-lived control ticket before any output;
6. deliberately does not auto-merge.

For a control-repository failure, the checkout-free sentinel routes the diagnosis to the existing highest DeepSeek Supervisor instead of trying to edit its own workflow inline. This keeps the observer independent while preserving the established repair validation and delivery path.

Repository Validation, control-plane CI, and applicable self-tests must pass before a repair PR is merged. DeepSeek has the highest technical decision priority; deterministic CI remains the write-safety gate.

## Ticket confidentiality

The raw signed control ticket is used only in memory for the work-repository dispatch. The production wrapper captures the legacy controller output, removes the raw ticket, and publishes only a non-replayable SHA-256 receipt and metadata. The workflow performs a second defensive redaction before summary and Artifact upload.

## Final review rule

A GitHub `success` conclusion means only that execution completed without a terminal runner error. It is not publication approval. `START`, `RESTART`, and explicit `REVIEW` publish only when the DeepSeek result says `APPROVE`.

## Physical limits

This design survives failure of the `test` workflow, local scripts, dependencies, result publication, and the normal control workflow because independent failure sentinels and rescue paths remain available. GitHub hosted Actions does not expose a user-defined priority queue; highest priority is enforced logically through mandatory entry and recovery routing. The system cannot operate when GitHub Actions itself is unavailable, required tokens are revoked, or the official DeepSeek API is unreachable. Those conditions must result in a truthful stop.
