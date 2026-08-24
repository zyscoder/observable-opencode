# Root-Cause Analysis Skill Final Review Fix B

Date: 2026-08-24

Branch: `codex/trace-stability-integration`

## Scope

This fix closes the pressure-test isolation gap without changing OpenCode
runtime behavior, Trace collection, `trace_query.py`, or the paused Python
attribution module. It covers all seven root-cause fixtures and makes the
boundary between Agent-visible inputs and evaluator-only labels explicit.

## TDD Evidence

The first focused RED run produced the intended failures:

- five fixtures were rejected because the builder supported only
  `known-root` and `ambiguous`;
- the two supported bundles lacked the canonical Skill and runtime-specific
  prompts;
- an Artifact digest mismatch was accepted;
- the forward wrapper lacked an external-binary isolated-cwd path;
- the implementation plan still contained three stale dollar-prefixed Skill
  invocations.

A second RED required a tracked forward runner instead of a documentation-only
wrapper. A final RED required sanitizing inherited repository navigation state
from the OpenCode child environment. Each RED was followed by focused GREEN
before the next behavior was added.

## Historical Isolation Contract

This report predates Fix B round 1. Its external-binary cwd path is now
classified as smoke only; cwd separation does not provide scored filesystem
isolation. Scored runs now require Docker or Podman plus an external Linux
release `OPENCODE_BIN`, as documented in the round-1 report.

`prepare_isolated_bundle.py` now accepts all seven case IDs. Before creating a
destination it validates terminal Causal IR identity, canonical node payload
and source hashes, declared Artifact SHA-256 digests, source containment, and
the absence of symlink traversal. Destination creation is no-overwrite.

Each Agent workspace contains exactly:

- the selected finalized `trace.json`;
- only Artifacts declared by that Trace;
- the canonical `.claude/skills/rootcause-analysis` package;
- `prompt-claude.md` with `/rootcause-analysis`;
- `prompt-opencode.md` with the OpenCode `skill` tool instruction.

It contains no `cases.json`, rubric, hidden `expected` object, sibling fixture,
report, Git history, or symlink to the repository. Both prompts contain the
exact selected question and isolated Trace path, but no expected-answer keys or
repository path.

The historical runner required `--opencode-bin` or `OPENCODE_BIN`, created
each bundle through the builder, and launches that executable with the bundle
as cwd. HOME, PWD, and XDG roots point outside the workspace into case-local
runtime state; inherited `OLDPWD`, `INIT_CWD`, `GIT_DIR`, and `GIT_WORK_TREE`
are removed. Safe-signal auditing, no-overwrite roots, per-case error records,
and continuation remain useful smoke evidence. These results are unscored.

## Evaluator Boundary

The builder is evaluator-side. It reads only `fixture` and `question` from the
case catalog for bundle construction and never copies `expected`. After Agent
execution, only the evaluator reads `pressure/cases.json` and `rubric.json` to
score captured output stored outside the Agent workspace.

Repository Bun/source execution is now documented only as Skill-discovery or
provider-preflight smoke testing. The preserved Claude authentication failure,
seven OpenCode provider failures, and round-3 signal spot check remain
unscored; no historical record or model result was rewritten.

## Audits

All seven bundles were built under
`/tmp/rootcause-final-review-fix-b-all7-20260824`. The explicit tree audit found:

- no symlinks;
- no `cases.json`, `rubric.json`, `.git`, or `reports` entry;
- no hidden expected-answer key in either runtime prompt;
- one selected Trace per workspace, one canonical Skill copy per workspace,
  and only the known-root declared Artifact.

The exact allowlist, question equality, prompt path isolation, Skill byte
identity, Trace byte identity, no-overwrite behavior, symlink rejection, and
digest rejection are also deterministic test assertions.

The synthetic external-binary spot check used:

```bash
python3 tools/rootcause_skill_tests/pressure/run_isolated_opencode.py \
  --batch-root /tmp/rootcause-final-review-fix-b-spot-20260824T120000Z-5a30 \
  --case known-root \
  --opencode-bin /bin/echo \
  --timeout-seconds 2 \
  --signal-grace-seconds 1
```

It exited `0`, built the isolated workspace, recorded that workspace as `cwd`,
used its `trace.json` and OpenCode prompt, reported `raw_returncode=0`,
`timed_out=false`, and `wrapper_error=null`. This is a wrapper/cwd spot check,
not an Agent inference result and not a rubric score.

## Verification

- Root-cause deterministic suite: 100/100 passed.
- Builder, runner, and `trace_query.py` bytecode compilation: passed.
- Official Skill validator: passed, `Skill is valid!`.
- Seven-bundle explicit build and tree/content audit: passed.
- Fixture immutability, repository whitespace, and scope diff checks: passed.

## Remaining External Prerequisite

A real scored result requires a working Docker or Podman daemon, an external
Linux release OpenCode binary, and a configured provider. The historical cwd
and blocked attempts do not satisfy that requirement.
