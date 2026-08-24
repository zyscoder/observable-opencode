# Root Cause Analysis Skill Final Review Fix B Round 1

Date: 2026-08-24

Branch: `codex/trace-stability-integration`

## Corrected Boundary

An isolated working directory is not scored filesystem isolation. Local and
source executions are now explicit `smoke` runs and always remain unscored.
Scored mode refuses to start unless it has a working Docker or Podman daemon
and an external Linux release OpenCode ELF.

Each Agent-visible bundle uses a fresh `case-<uuid>` identity in its directory,
runtime prompt, Trace manifest, and node scopes. The builder recomputes
canonical node payload/source integrity and emits original/derived Trace hashes
plus fixture mapping only to evaluator-side stdout. Task facts and the exact
user question are preserved. Hidden expected outcomes and rubric data never
enter the bundle.

## Scored Mount And Environment Contract

The scored container receives exactly two read-only bind mounts:

- one opaque workspace at `/workspace`;
- one external Linux release binary at `/opt/opencode`.

The repository, batch root, audit JSONL, evaluator mapping, workspace parent,
sibling cases, and canary are not mounted. Prompts use `/workspace/trace.json`.
Only named provider variables from a fixed whitelist are forwarded; their
values are never placed in command argv or audit records. Accidental provider
value echoes in child stdout/stderr are redacted in the host audit projection
without altering the child environment. An adversarial probe
must confirm parent/evaluator/audit/canary paths are unavailable before Agent
inference.

## Current Integration Status

Structural command, mount, opaque identity, provenance, secret-redaction, and
refusal tests are deterministic. The current host has `/opt/homebrew/bin/docker`
but `docker info` fails with permission denied on
`/Users/zys/.colima/observable-benchmark/docker.sock`; Podman is absent. The real
scored container integration is therefore BLOCKED, not passed. Historical local
attempts remain smoke evidence only.

## TDD And Audit Evidence

RED tests first demonstrated missing opaque identity arguments, unchanged
semantic Trace identities, absent smoke/scored modes, absent container mount
construction, and audit secret echo. GREEN implementation then established:

- all seven no-overwrite opaque bundles with recomputed node integrity;
- evaluator-only source/derived hashes and fixture mapping;
- exactly two scored bind mounts and `/workspace` prompt paths;
- explicit refusal without a container daemon or Linux ELF;
- host-side stdout/stderr secret redaction;
- structural parent/evaluator/audit/canary denial checks.

The explicit seven-bundle audit at
`/tmp/rootcause-fix-b-round1-all7-20260825-02` built 7/7 bundles with zero
identity/evaluator leaks and no symlinks. The smoke spot check at
`/tmp/rootcause-fix-b-round1-smoke-20260825-01` returned
`run_kind=smoke_local`, `evaluation_status=unscored_smoke`, and exit `0`.

Deterministic verification: 106/106 tests passed with one scored integration
test explicitly skipped as BLOCKED by the inaccessible Docker daemon;
`py_compile`, official Skill validation (`Skill is valid!`), fixture
immutability, and `git diff --check` passed.

## Superseded Security Details

Fix B round 2 supersedes this report's simple ELF-magic and raw-string
redaction guarantees. The current protocol requires a pinned image digest,
expected release hash, full ELF64/architecture validation, forced entrypoints,
secret-free version preflight, strict mount paths, and recursive multi-encoding
secret redaction. See the round-2 report.
