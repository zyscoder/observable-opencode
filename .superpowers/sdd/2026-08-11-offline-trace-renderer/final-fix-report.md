# Offline Trace Renderer Final Fix Report

**Date:** 2026-08-11
**Scope:** Single final-review fix round for the offline trace renderer migration
**Commit subject:** `fix(trace-renderer): close final review findings`

## Result

All Critical, Important, and Mermaid Minor findings from `final-review.md` are addressed in one TDD round. Runtime collection remains passive, attribution remains semantic-Trace-only, and the renderer continues to depend on the shared OpenCode Causal IR implementation without a reverse production dependency.

## Fixes

### 1. Output collision protection

- The CLI constructs a protected set containing the selected semantic input, every standard live/terminal semantic file, safe manifest-declared files, and every safe referenced artifact path.
- Explicit outputs are rejected before artifact snapshot publication or HTML generation when they directly equal a protected path.
- Existing symlink, parent-resolution, and hard-link aliases are rejected by realpath and device/inode identity under the accepted exclusive-output-directory ownership model.
- Tests cover `trace.json`, `records.jsonl`, `manifest.json`, another terminal JSON file, a referenced artifact, hard-link aliases, and a symlink alias.

### 2. Compact finalized-journal integrity

- One shared Causal IR finalization hash now canonically covers every complete node, edge, artifact, and diagnostic object plus the canonical trace envelope, including all manifest, journal, metrics, and compatibility fields.
- Finalized replay verifies the terminal entry payload hash, terminal position/sequence, canonical journal summary, preceding journal payload hash, run/case/entity identity, `finish` record type, manifest payload binding, graph counts, replay identity, and full finalization hash.
- Tamper tests independently alter node semantics, edge semantics, artifact metadata/path, diagnostics, canonical manifest, canonical metrics, terminal payload hash, sequence, run, case, entity, record type, and journal summary binding.

### 3. Strict journal loading

- Shared Causal IR journal validation rejects unknown operations, missing common entry fields, non-contiguous sequences, inconsistent run/case identity, malformed canonical nodes/edges/artifacts/diagnostics/checkpoints/finalizations, entity mismatches, and current journals without the initial `run.start` node.
- Renderer errors preserve the source `records.jsonl:<line>` location.
- Valid journal prefixes still replay as visibly incomplete recovery output.
- Projection-only historical `trace.json` compatibility loading remains unchanged.

### 4. Stable case-directory reuse

- Before writing any current-run JSONL, trace startup invalidates prior `trace.json`, manifest, compatibility projections, terminal partial output, and stale offline `trace.html` in the stable case directory, then fsyncs the directory.
- Artifacts remain semantic payload storage; runtime still generates no HTML and no periodic full snapshot.
- The repeated-case regression creates a completed old run, starts a new live run with the same case ID, proves old terminal/derived outputs are absent, checks the new run identity and journal contents, sends `SIGKILL`, and verifies the renderer selects current `records.jsonl` as incomplete recovery.
- Agent-visible prompt, message, and tool-result byte preservation remains covered by the runtime suite.

### 5. Documentation

- README live file semantics now list append-only JSONL plus artifacts.
- `partial/latest.json` is documented as terminal compatibility output created only after graceful/catchable finalization.
- `records.jsonl` is documented as the sole current-run recovery source after `SIGKILL`.
- Stable case-directory invalidation and stale `trace.html` handling are documented.
- The Mermaid HTML node now uses a distinct `HT` identifier.

## TDD Evidence

Initial focused renderer run after adding tests:

- `20 pass`, `6 fail`; failures were the intended missing integrity, structural-validation, and collision behaviors.

Initial repeated-case observability run:

- Failed because old terminal output was still present during the second live run.

After implementation, the same focused cases passed before broader verification.

## Verification

- Renderer full suite: `50 pass`, `0 fail`, `298 expect()` calls.
- Focused Causal IR/runtime/publication suites: `88 pass`, `0 fail`, `7539 expect()` calls.
- Curated full observability suites: `312 pass`, `0 fail`, `9832 expect()` calls across six files.
- Root typecheck: `15 successful`, `15 total`.
- `git diff --check`: exit 0.

A recursive `bun test test/observability` diagnostic run produced `337 pass`, `9 fail`, and `8 errors`. Eight errors are intentionally red stress fixture executables discovered recursively; the remaining failure is the previously documented sandbox-sensitive FeatureBench port-0 server test. The explicit six-file observability gate excludes fixture payloads and passed completely.

## Scope Audit

Modified production files are limited to:

- `packages/opencode/src/observability/causal-ir.ts`
- `packages/opencode/src/observability/case-trace.ts`
- `packages/trace-renderer/src/load.ts`
- `packages/trace-renderer/src/cli.ts`

Tests and documentation are limited to the corresponding renderer/observability suites and root README. No Agent prompt, model request, tool execution, attribution, release workflow, or reverse dependency code changed. Unrelated untracked report files were preserved.

## Remaining Concerns

- The accepted same-privilege pathname mutation race remains out of scope; checks cover direct paths and existing static aliases under exclusive output-directory ownership.
- Projection-only historical `trace.json` compatibility intentionally lacks canonical journal integrity metadata and remains distinguishable from finalized Causal IR replay.
- The recursive observability directory command remains unsuitable as a release gate because it discovers intentionally failing fixture programs and includes the environment-sensitive port-0 benchmark test.
