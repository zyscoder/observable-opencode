# Root Confirmation Task 2 Fix Wave 29 Brief

Base commit: `2b05451f7`

## Scope

Close all three Important findings from the fourth cumulative Task 2 review.
Keep the attribution process offline, passive, read-only, and unable to feed
results back to the Agent. Do not implement Task 3 or add dependencies.

Use strict TDD. Record focused RED before production changes and GREEN after
each fix. Finish with affected suites, full attribution discovery,
`compileall`, and `git diff --check 2b05451f7..HEAD`.

## Required Fixes

### 1. `no_defect` must close every open authored candidate

A Global Candidate Judgment may return `no_defect` only when every open
authored root-eligible candidate has a conclusive assessment that rules it out.

Requirements:

- every `request.open_authored_root_candidate_refs` entry must have exactly one
  assessment;
- its output defect status must be known and `absent`;
- `unknown` defect status, input/output status, causal role, missing
  counterfactual, or unresolved causal path cannot coexist with `no_defect`;
- a present candidate may not be hidden behind `causal_role="unknown"` or any
  non-root role;
- decisive absent exculpatory/outcome evidence remains required;
- evidence insufficiency must produce a schema rejection so the existing
  repair path returns `needs_expansion` or `inconclusive` with a concrete
  missing-evidence item; it must never mark the seed no-defect.

Add tests for unknown output, unknown role, absent assessment missing one open
candidate, present status under an unrecognized role, and a fully closed valid
`no_defect` matrix. Cover real adapter repair and analyzer seed outcome.

Version the Global Judge prompt/schema and persisted validation envelope if the
accepted meaning changes.

### 2. Use one complete marker and canonical owner classifier

Define one shared predicate for any global pass/failure-bearing record. It must
recognize:

- `kind="global_candidate_pass"`;
- `global_pass_identity`;
- `pass_identity`;
- `failure_projection`;
- any other global terminal identity field defined by the exact schemas.

Use it for investigation journal records and unresolved episodes before any
filtering, terminal-set derivation, stale-seed quarantine, restore, report, or
evaluator reconciliation.

For every completed/failed pass:

- reconstruct the expected seed builder identity from `seed_ref` and
  `defect_fingerprint`;
- require `seed_binding_identity` and `pass_identity` to match that binding;
- reconstruct the complete canonical `_global_pass_owner`, including
  hypothesis, visit, occurrence, and seed fields;
- require exact equality, not only matching `seed_binding_identity`;
- reject kind-only episodes, owner substitution, unknown status, incomplete
  markers, and extra semantic fields before stale state can be removed.

Add checkpoint, report, evaluator, live terminal-set, and stale-quarantine
tamper tests. Include an otherwise stale record with a substituted owner to
prove validation happens before filtering.

Version the global terminal-record/failure persistence identity if needed.

### 3. Preserve explicit artifact ownership through terminal confirmation

Terminal confirmation validation must use the exact artifact evidence
envelopes persisted in the queue and action. It must not re-run a generic
ownerless artifact eligibility probe for an already disambiguated artifact.

Requirements:

- map every artifact `confirmation.evidence_refs` entry to exactly one
  queue/action artifact envelope;
- re-run `artifact_evidence_envelope(..., expected_owner_ref=<persisted owner>)`
  and require exact content/hash/range/path/provenance/owner-binding equality;
- node evidence continues to use normal active-revision eligibility;
- missing, duplicate, substituted, stale, or contradictory artifact envelopes
  fail closed for only the owning seed;
- validate the full terminal confirmation before writing a completed/failed
  checkpoint action;
- if a provider result fails terminal artifact validation, persist a canonical
  `unknown`/evidence-gap confirmation for that seed, do not leave a completed
  action, do not raise out of the analyzer, and allow other seeds to finish;
- a valid multi-active-owner artifact with an explicit candidate/path owner
  must complete and survive checkpoint/report/evaluator round-trip.

Avoid adding a second artifact-owner contract. Reuse the graph's canonical
envelope and the queue/action envelope already introduced in Fix26/Fix27.

## Verification

At minimum:

1. New Fix29 focused tests.
2. Fix17-Fix29 plus global Judge, confirmation, artifact, seed, checkpoint, and
   recursive core regressions.
3. Report/evaluator/replay/acceptance affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check 2b05451f7..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix29-report.md` with root-cause
  validation, RED/GREEN evidence, schema/policy decisions, exact test
  commands/counts, changed files, and residual risks.
- One commit containing only Fix29 files, this brief, and report. Do not touch
  or stage unrelated untracked files.
