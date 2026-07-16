# Final Claim Evidence Closure Iteration

## Scope

- Trace core: Causal IR 1.0 / semantic trace 6.0
- Agent path: `opencode serve -> POST /session -> POST /session/:id/message`
- Model: DeepSeek `deepseek-v4-pro`
- Stress case: conflicting current and obsolete discount requirements, repository edit, and post-change test
- Attribution: offline backward semantic taint, eight levels and 48 nodes per defect branch, one-hour judge timeout
- Collection mode: passive sidecar; trace-derived facts never enter Agent prompts, tool inputs, or execution state

## Problem Reproduced

The v4 stress trace contained one mixed final claim: the discount cap was changed
from 20% to 15% and the test passed. Its `change` candidate scored 0.72 but was
recorded as `rejected_lower_ranked_match`.

The matcher was not failing. The candidate selector classified the whole claim
as one primary kind, `verification`, and then retained only verification facts.
This made `responseClaimKind()` an accidental evidence gate and dropped a
high-confidence change record from the final causal chain.

## Implementation

1. Reconstruct grounding candidates from the tool outcomes confirmed in the
   final model-request context.
2. Canonicalize tool-result aliases before they become Claim candidates.
3. Persist every candidate decision with score, reasons, origin, rejection
   reason, `agent_attention_observed=false`, and `behavior_impact=none`.
4. Create attribution-eligible Claim edges only for selected candidates.
5. Match percentages, exact code identifiers, change diffs, paths, intent, and
   structured verification semantics.
6. Select mixed change-plus-verification Claims by independent semantic lanes:
   one change candidate, one verification candidate, and the strongest
   remaining facts. The primary `claim_kind` remains a presentation field and
   no longer suppresses secondary claim semantics.
7. Keep derived response and observation nodes from being promoted to defect
   roots merely because all inspected predecessors were non-defective.
8. Allow a genuine first-observed propagation boundary to become a provisional
   introduction candidate only after all declared predecessors are confirmed
   absent and root confirmation accepts it.

## Automated Verification

- CaseTrace: 130 passed, 0 failed
- Causal IR: 53 passed, 0 failed
- Offline attribution: 97 passed, 0 failed
- TypeScript typecheck: passed
- Mixed Claim regression: passes without explicit response source refs
- Rejected unrelated generation-context evidence: no Claim edge

## Real HTTP Stress Result

The v5 run completed successfully in 44.5 seconds. It produced 266 semantic
records, 700 Causal IR edges, a 21 MiB trace directory, and both `trace.json`
and `trace.html` after signal-driven server shutdown.

The Agent selected `docs/current-requirement.md`, rejected the obsolete 20%
design, changed `src/pricing.mjs` from 0.2 to 0.15, and ran `npm test`
successfully. All five final Claims had direct support; none was unsupported or
marked as a weak evidence match. The code-change Claim has a confirmed edge
from `change:chg_1_eacf7327`, and the verification Claim has a confirmed edge
from `verification:ver_2_9ccb23d1`.

## Attribution Result

The offline module analyzed five independent final-Claim branches:

- Outcome: `no_defect`
- Root causes: 0
- Judge errors: 0
- Unresolved refs: 0
- Depth/node limits hit: false
- Message-lineage gaps: 0

This corrects the earlier false-root behavior. A response Claim that truthfully
describes a pre-existing repository conflict, the selected current requirement,
the repair, or a passed verification is treated as an observation of the task
outcome rather than the component that introduced a defect.

## Critical Findings

The trace is now sufficient to answer which requirement was read, which one was
selected, what code changed, what verification ran, and which evidence supports
each final statement. Candidate rejection is inspectable without contaminating
the causal graph, and the attribution module can distinguish repository defects
from Agent-introduced defects.

One semantic precision issue remains. The final post-change verification Claim
also selected a pre-change failed verification fact as direct support. The
offline judge correctly interpreted it as baseline context, but the fact layer
should encode that relation explicitly and exclude superseded verification from
current-result direct support.

The remaining bulk is mostly repeated message snapshots, context membership,
and forensic stream events. These are useful for replay but should stay outside
the default attribution projection; this iteration does not delete them because
the current objective is causal correctness, not storage compaction.

## Next Iteration

1. Add a failing regression with pre-change failure and post-change success
   facts present in the same final generation context.
2. Select verification evidence by repository revision, stage, effective final
   state, and observed ordering before lexical similarity.
3. Preserve the failed baseline as contextual/superseded evidence with a
   non-attribution edge instead of direct support for a passed Claim.
4. Rerun one successful and one intentionally partial/failing benchmark case,
   then compare manual analysis with offline attribution.
5. After temporal correctness is stable, compact repeated context and message
   projections behind lazy HTML expansion without changing Causal IR identity.
