# Attribution Convergence vNext Benchmark Results

Date: 2026-08-01

## 1. Scope And Decision

This Stage D replay validates the current semantic Trace and offline recursive
attribution implementation without changing Agent-visible behavior. It covers:

- immutable open-source FeatureBench Pydantic and Seaborn traces;
- zero-Provider candidate discovery, clustering, and root-membership checks;
- strict re-evaluation of historical reports;
- an authorized DeepSeek request against the sanitized Sphinx fixture;
- full Python and core observability regressions;
- the HTTP benchmark runner shutdown path used by real case execution.

The current decision is **partial acceptance, Task 7 remains pending**. Candidate
recall and deterministic regressions pass. Live root/factor quality cannot yet be
accepted because the configured DeepSeek account returned HTTP 402
`Insufficient Balance`, and the Pydantic payload did not have explicit external
transmission authorization at execution time.

## 2. Frozen Inputs

| Input | SHA-256 | Role |
| --- | --- | --- |
| Pydantic trace | `97d48e13df5b7113d99fd40637b298721f086b7a7b028c8dc31aa20be977088e` | Attribution source |
| Pydantic labels | `eb1cbdb0a5dd7fe17945096b1fbdf1e1c74940c951a1cf9c040aab10bb6dc9ad` | Offline scoring only |
| Seaborn trace | `9d9aeacf8df828368ca837fe2de74f09da72edadb78ea6700b9d6cd96f3ee94f` | Attribution source |
| Seaborn labels | `55ebe455c3e9ff4ba76cba7c47270c9dc03cc10fd629917b3a90039a50adfdaa` | Offline scoring only |
| Sanitized Sphinx fixture | `e4042a59185ed08854011f7bf6d5275a1790032dfde102c6c24ebc7632cb4746` | Authorized live smoke test |
| Sphinx labels | `b3298456344596bd800e8e000af9c24d4a8a6f8bf4c49b7501e43a58bda01c98` | Offline scoring only |

The machine-readable input binding is stored at
`.benchmark-runs/attribution-convergence-vnext-20260731/input-manifest.json`.
Human labels were loaded by the zero-Provider evaluation script and strict
evaluator only; they were not passed to the attribution CLI.

The manifest binds the authoritative `partial/latest.json` files. Their former
`trace.json` compatibility aliases disappeared during later finalization, while
the authoritative files retained the exact frozen sizes and hashes. Acceptance
therefore does not depend on a transient compatibility alias.

## 3. Zero-Provider Candidate Replay

Three independent manifest builds were executed for each large trace.

| Metric | Pydantic | Seaborn |
| --- | ---: | ---: |
| Passes | 3 | 3 |
| Human roots | 1 | 4 |
| Minimum roots discovered | 1 | 4 |
| Minimum roots offered | 1 | 4 |
| Root group coverage | 100% | 100% |
| Original Trace records | 2,106 | 2,403 |
| Discovered candidates | 537-539 | 545 |
| Offered candidates | 256 | 256 |
| Discovered-candidate reduction vs records | 74.41-74.50% | 77.32% |
| Offered-candidate reduction vs records | 87.84% | 89.35% |
| Clusters | 174-176 | 176 |
| Offered clusters | 134 | 141-142 |
| Existing strict pages | 32 | 32 |
| Cluster directory pages | 17 | 18 |
| Catalog reduction | at least 47.66% | at least 44.53% |
| Simulated expansion root recall | 100% | 100% |
| Provider requests | 0 | 0 |

The candidate subsystem therefore satisfies the primary safety condition: no
labeled root is lost before an LLM call. It also cuts the catalog presented for
triage by roughly 45-48%.

The design target requires unique discovered candidates to be reduced by at
least 85% relative to original Trace records. Pydantic reaches only
74.41-74.50% and Seaborn 77.32%, so this target **fails** at discovery. The
offered 256-candidate cap exceeds 85% reduction, but it is a later budget gate
and cannot be substituted for the unique-discovery metric.

Every manifest member was resolved against the loaded graph after review-seed
injection: 540 unique Pydantic refs and 548 unique Seaborn refs across the three
passes produced zero dangling references.

This run does **not** prove the `<=16` strict-page target. The zero-Provider tool
builds the shadow manifest but does not execute the Cluster Triage Judge, so the
current full-original strict fallback remains 32 pages. Successful live triage,
bounded expansion, and final strict-page count remain pending.

Artifacts:

- `.benchmark-runs/attribution-convergence-vnext-20260731/pydantic-zero-provider-clustering.json`
- `.benchmark-runs/attribution-convergence-vnext-20260731/seaborn-zero-provider-clustering.json`

## 4. Historical Baseline Reconciliation

The current strict evaluator rejected both historical reports instead of
silently adapting their schemas:

- Pydantic: `strict_report_action_reconciliation` because restored global-pass
  metadata contradicted authoritative persisted actions.
- Seaborn: top-level schema mismatch because the historical report lacks the
  required v22 fields and contains legacy-only fields.
- Sphinx R6: `strict_report_invalid` because the confirmed-root summary used an
  inexact historical schema.

These reports remain qualitative baselines only. Validators and human labels
were not weakened to manufacture a numeric comparison.

## 5. Live Provider Result

### 5.1 Pydantic

No external request was made. The attempted command was rejected before
process creation because the existing authorization covered Sphinx data but did
not explicitly cover this Pydantic trace and review payload.

### 5.2 Sanitized Sphinx

One physical request reached the authorized Anthropic-compatible DeepSeek
endpoint. The provider returned HTTP 402 `Insufficient Balance`.

The analyzer response was structurally correct:

- `analysis_outcome`: `inconclusive`;
- physical requests: 1;
- provider circuit: open after the deterministic non-retryable failure;
- unresolved seed: `record:observed`;
- confirmed roots: none;
- fabricated root claims: none;
- strict root recall and precision: 0 because no semantic judgment completed.

This validates provider-failure semantics, but not attribution quality. The
result and strict comparison are stored under
`.benchmark-runs/attribution-convergence-vnext-20260731/sphinx-live/`.

## 6. Regression Verification

| Verification | Result |
| --- | --- |
| Python attribution suite | 1,559 passed, 0 failed |
| Complete configured observability Bun suite | 300 passed, 0 failed |
| Python compileall | passed |
| `git diff --check` | passed |
| Credential scan for supplied keys | no repository matches |

Environment: macOS Darwin 25.5.0 arm64, Python 3.9.6, Bun 1.3.14. The
machine-readable manifest binds Git HEAD and a content hash over the complete
observability and attribution source/test scope, including untracked files.
The 184-file, path-sorted, per-file digest list is preserved as
`.benchmark-runs/attribution-convergence-vnext-20260731/source-snapshot.sha256`.
Commands, counts, durations, and environment are also recorded in
`.benchmark-runs/attribution-convergence-vnext-20260731/verification-summary.json`.

The configured suite explicitly enumerates all six formal test files: four
top-level observability files, the stress harness, and the FeatureBench runner.
Recursive directory discovery is not the gate because it also executes eight
fixture `test/*.mjs` files whose failing assertions are the benchmark inputs.

During verification, the FeatureBench HTTP test exposed a Bun-specific cleanup
hang: the Node HTTP request completed but a persistent test connection delayed
server teardown. The fixture now returns `Connection: close`, stops accepting
new connections, closes active connections, and asserts `server.listening` is
false without using `unref()`. A separate no-server branch proves the fake
global fetch is never called. Production Runner code is unchanged.

## 7. Acceptance Matrix

| Target | Status | Evidence |
| --- | --- | --- |
| Frozen trace/label identities | Pass | Hash manifest and recomputation |
| Fabricated or dangling manifest refs | Pass | Pydantic 0/540; Seaborn 0/548 |
| Human-root candidate membership recall = 1.0 | Pass | Pydantic 1/1; Seaborn 4/4 across all passes |
| Clustering does not remove a labeled root | Pass | Offered-root and simulated-expansion recall = 1.0 |
| Unique candidates reduced >=85% vs Trace records | **Fail** | Pydantic 74.41-74.50%; Seaborn 77.32% |
| Pydantic successful-triage strict pages <=16 | Pending | Live triage unavailable; pre-triage fallback is 32 pages |
| Root precision and recall >=0.85 | Pending | Provider returned 402 before judgment |
| Multi-root coverage >=0.90 | Pending | No completed large live judgment |
| Factor-role F1 >=0.75 | Pending | No completed large live judgment |
| Healthy-Provider inconclusive rate <=10% | Pending | Provider was not healthy |
| Successful partial-judgment retention =100% | Pass in deterministic tests; live pending | Full checkpoint suite passed; no successful page preceded the live 402 |
| Completed replay uses zero new Provider requests | Pass in deterministic tests; live pending | Full checkpoint suite passed; no completed new live run |
| Non-retryable Provider failure remains factual and bounded | Pass | One request, open circuit, inconclusive report, no fake roots |
| Agent-visible message/tool/provider/final-answer hashes unchanged | Pending for real benchmark | Synthetic passive-isolation regression passed; no before/after real-case hash set |
| Pydantic-scale peak RSS <=2 GB | Pending | Synthetic 5,000-record run ended at about 772 MB RSS; no Pydantic peak measurement |
| SIGINT/SIGTERM HTML finalization <=60 seconds | Pending for real benchmark | Synthetic signal tests passed in under one second; no fresh large-case timing |

## 8. Next Iteration

1. Restore DeepSeek account balance and explicitly authorize the open-source
   Pydantic trace, review facts, and attribution prompts for the configured
   endpoint.
2. Resume the isolated Pydantic cold run with 3600-second request timeout and
   320-request ceiling. Do not pass label files to the attribution CLI.
3. Strictly score root precision/recall, Top-1, multi-root coverage, factor-role
   F1, unknown rate, forbidden roots, physical requests, and first mismatch.
4. Replay the completed checkpoint and require zero additional Provider calls.
5. If live triage still exceeds 16 strict pages, inspect directory judgments and
   expansion provenance before changing budgets or clustering policy.
6. Run a second live large case only after Pydantic completes without a Provider
   or circuit blocker.
7. Copy immutable benchmark inputs and raw command logs into a persistent,
   content-addressed run bundle instead of relying on `/private/tmp` aliases.
