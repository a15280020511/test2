# Tool Package Guardrails Policy

## Purpose

This policy defines what `a15280020511/test2` is responsible for when using external tool packages and, equally important, what it is **not** responsible for.

The repository intentionally relies on mature, authoritative, actively maintained upstream projects. Their maintainers own the package's upstream roadmap, releases, bug fixes, security fixes, and continuing development. `test2` must not duplicate that maintenance burden.

## Core rule

**Upstream package maintenance belongs to the upstream maintainers. `test2` only owns safe integration, stable execution, fault isolation, evidence, cleanup, and compatibility at the repository boundary.**

Do not build a second package-maintenance platform inside `test2`.

## Upstream selection standard

Prefer tool packages that are:

- maintained by authoritative, high-quality teams or widely trusted expert communities;
- actively developed and supported upstream;
- suitable for long-term use;
- available from the official upstream repository or official package distribution channel;
- technically appropriate for the concrete task.

Web GPT chooses tools according to the specific task. Do not hard-code one universal tool stack when different tasks require different capabilities.

## What `test2` does NOT own

`test2` does not take responsibility for:

- maintaining an upstream package's source code;
- reproducing the upstream project's release process;
- maintaining a permanent fork unless explicitly approved as an exceptional case;
- building a separate background service to watch, mirror, or continuously rewrite upstream packages;
- replacing the upstream team's issue tracker, security process, release notes, or development roadmap;
- modifying upstream package internals merely to make them conform to `test2` preferences.

The fact that an upstream package has a newer release is not by itself a repository fault.

## What `test2` DOES own

### 1. Safe integration boundary

- Install and invoke packages only through controlled repository workflows.
- Prefer official package names, official distributions, and documented interfaces.
- Do not silently execute unknown third-party code outside the intended package boundary.
- Keep repository-specific adapters thin and replaceable.

### 2. Permission and secret boundary

- A tool package receives only the permissions required for the current task.
- Repository secrets must never be printed, exposed, copied into artifacts, or written into source files.
- Packages must not receive write access to protected repository areas unless the workflow explicitly requires and authorizes it.

### 3. File-system boundary

- Do not let tool packages directly modify `.git/`, `tests/`, generated `artifacts/`, `runtime_results/`, or production source without an authorized workflow.
- Temporary files must stay in task-scoped working locations whenever practical.
- Source-code changes remain subject to the repository's repair and verification rules.

### 4. Runtime and resource boundary

- Apply task-appropriate timeouts and bounded resource use.
- A tool package failure, hang, malformed output, or dependency conflict must not be allowed to corrupt unrelated repository state.
- Failures should be isolated to the current operation whenever possible.

### 5. Failure isolation

- One package failure must not silently invalidate unrelated tools or previous verified results.
- Treat package/API failures as explicit operational evidence.
- Do not fabricate successful execution when a package did not run successfully.

### 6. Evidence and observability

- Important tool execution must produce enough status, logs, or result evidence to support diagnosis.
- Distinguish upstream failure, local integration failure, invalid input, and environment failure whenever evidence permits.
- Results used for decisions must remain traceable to the operation that produced them.

### 7. Temporary-data cleanup

- Task-scoped temporary data should be deleted after it is no longer needed, except for intentionally retained audit evidence.
- Do not create a permanent local data platform merely because a package can persist data.

### 8. Compatibility responsibility

DeepSeek Steward owns **repository-side compatibility and integration problems**, including:

- incorrect package invocation;
- broken adapters;
- incompatible repository configuration;
- dependency conflicts inside `test2`;
- workflow integration failures;
- upstream API/interface changes that require a local adapter change.

DeepSeek Steward does **not** become the long-term maintainer of the upstream package itself.

When an upstream release breaks `test2`, repair the smallest repository-side integration surface needed to restore compatibility. Do not fork or rewrite the upstream project by default.

## Update policy

Upstream packages continue to be maintained and updated by their upstream teams.

`test2` does not need its own package-updater or upstream-maintenance subsystem. Repository work is required only when:

- the current integration becomes incompatible;
- a required security or compatibility change must be adopted;
- the task explicitly needs a newer upstream capability;
- the current package version prevents correct repository operation.

In those cases, DeepSeek Steward handles the `test2` compatibility work under the normal repair, verification, and recovery process.

## Role boundary

- **Web GPT**: decides which tool or combination of tools is appropriate for the concrete task and controls task-level intent.
- **DeepSeek Steward**: manages repository-side tool integration, compatibility, runtime faults, and repair.
- **GitHub**: provides controlled execution, logs, evidence, verification, and repair delivery.
- **Upstream maintainers**: own the package itself, including ongoing development, releases, and upstream maintenance.

## Stability principle

Prefer a small number of mature upstream tools with thin integration layers over a large internally maintained tool platform.

**Do not duplicate upstream maintenance. Guard the boundary; do not become the upstream.**
