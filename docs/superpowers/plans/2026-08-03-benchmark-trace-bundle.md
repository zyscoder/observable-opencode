# Benchmark Trace Bundle v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development`. Capture RED and GREEN evidence. Do not
> commit or push.

**Goal:** Publish an immutable, content-addressed, self-contained benchmark
input closure that can replay after all source paths disappear.

**Architecture:** Pure composition, secure bundle storage, and common case
loading are separate modules. Bundle mode always uses an explicit artifact root
and full SHA-256; legacy raw mode remains a compatibility shim.

**Constraints:** Python 3.9+, offline build/open, labels evaluator-only, no
incomplete production mode, preserve the dirty worktree.

---

### Task 1: Pure Ordered Composition

**Files:**
- Create: `tools/trace_attribution/trace_attribution/benchmark_composition.py`
- Create: `tools/trace_attribution/tests/test_benchmark_bundle.py`

- [ ] Add RED tests for Trace -> review -> evaluations in caller order, canonical
  output, immutability, duplicate artifact IDs, and no filesystem/artifact read.
- [ ] Implement `compose_effective_trace()` as a pure JSON transformation.
- [ ] Run GREEN and prove evaluation reordering changes the binding when facts
  are order-sensitive.

### Task 2: Exact Immutable Bundle Contract

**Files:**
- Create: `tools/trace_attribution/trace_attribution/benchmark_bundle.py`
- Modify: `tools/trace_attribution/tests/test_benchmark_bundle.py`

- [ ] Add manifest RED tests for exact keys, canonical bundle ID, member order,
  complete 64-hex digests, unique artifact IDs, source/effective/labels roles,
  and attribution-role label isolation.
- [ ] Implement immutable `BenchmarkTraceBundle` and strict read-only
  `open_benchmark_trace_bundle()`.
- [ ] Run GREEN.

### Task 3: Secure Content-Addressed Builder

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/benchmark_bundle.py`
- Modify: `tools/trace_attribution/tests/test_benchmark_bundle.py`

- [ ] Add RED attacks for manifest-root poisoning, absolute/`..`/backslash and
  reserved paths, parent/leaf symlinks, FIFO/special files, duplicate paths,
  unknown refs, changed-during-copy files, and legacy prefix mismatch.
- [ ] Add fault injection proving no partially published bundle and no damage to
  an existing bundle ID.
- [ ] Implement explicit-root artifact validation, content-addressed copying,
  effective Trace rewriting, full staged validation, and publish-to-new-ID.
- [ ] Run GREEN; delete all source files and reopen successfully.

### Task 4: Thin Builder CLI

**Files:**
- Create: `tools/trace_attribution/scripts/build_benchmark_trace_bundle.py`
- Modify: `tools/trace_attribution/tests/test_benchmark_bundle.py`

- [ ] Add RED tests for `--trace`, required `--artifact-root`, optional review,
  repeated ordered evaluation, optional labels, and `--store`.
- [ ] Implement one-call CLI with no independent hashing or path logic.
- [ ] Run GREEN; invalid input exits 2 and exposes no bundle.

### Task 5: Common Bundle/Legacy Loader

**Files:**
- Create: `tools/trace_attribution/trace_attribution/benchmark_case.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- Modify: `tools/trace_attribution/tests/test_recursive_cli.py`
- Modify: `tools/trace_attribution/tests/test_recursive_benchmarks.py`

- [ ] Add RED tests for mutually exclusive bundle/raw modes, identical
  effective Trace in attribution/evaluation, explicit bundle artifact root,
  no call to manifest root inference, and corruption rejection before Provider.
- [ ] Implement `load_benchmark_case()`; keep `load_graph()` as legacy shim.
- [ ] Ensure attribution role never receives labels and evaluation role requires
  validated labels when scoring.
- [ ] Run focused GREEN.

### Task 6: Reproducibility And Security Acceptance

**Files:**
- Modify: `tools/trace_attribution/tests/test_benchmark_bundle.py`
- Create: `docs/superpowers/reports/2026-08-03-benchmark-trace-bundle-results.md`

- [ ] Build a durable Sphinx bundle from the clean flash probe inputs.
- [ ] Delete a temporary copy of every source path and reproduce graph identity,
  checkpoint replay, and strict comparison from the bundle.
- [ ] Change one byte in each member class and prove fail-closed behavior with
  zero Provider calls.
- [ ] Run full Python discovery and configured Bun observability tests; record
  exact counts and environment limitations.
