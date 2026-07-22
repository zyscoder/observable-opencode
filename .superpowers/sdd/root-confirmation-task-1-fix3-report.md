# Root Confirmation Stability Task 1 Fix Wave 3 Report

Base commit: `ffd06c872b82761bb38251c2d662f5910bbfc6ca`

## Scope

Implemented the four directed Task 1 fixes only. Attribution remains offline,
passive, read-only, and isolated from agent behavior. No dependency or Task 2
work was added.

## Fixes

1. Added a dedicated seed binding identity derived from `(start_ref,
   defect_fingerprint)`. It is carried through hypothesis identity,
   introduction bindings, confirmation queues, requests, confirmations,
   journals, and checkpoints without entering the semantic defect fingerprint.
   Shared-root seeds now retain independent hypotheses and confirmations, and a
   resumed run converges to the uninterrupted report without repeating a
   completed confirmation.
2. Enforced exact v3 start-ref coverage in direct report construction, parsing,
   and evaluator shape validation. Every report start ref must have a seed
   result, extra seed start refs are rejected, and multiple fingerprints for one
   start ref remain valid.
3. Centralized per-seed confirmation validation in report `__post_init__` so
   direct construction and `from_dict` enforce the same invariant. Every listed
   identity must exist at top level and independently bind through seed identity,
   recursive-path endpoint, defect lineage, and any published root. Extra or
   cross-seed identities fail closed.
4. Canonicalized duplicate retrieval routes independently of score. Rank still
   selects and orders candidate refs, but no longer changes candidate source or
   relation, hypothesis claim/identity/support facts, global Judge facts, or
   independent confirmation facts. Canonical routes are reconstructed from
   recorded graph edges when available, preserving authentic trace provenance
   confidence; retrieval-only confidence is omitted from factual payloads.

## TDD Evidence

Before production edits, directed regressions failed as expected:

- A real analyzer run with two start refs, one shared semantic defect
  fingerprint, and one shared root produced only one independent confirmation.
- Direct construction accepted a v3 report missing a declared start ref.
- Direct construction and parsing accepted extra missing or cross-seed
  confirmation identities when one valid identity was also present.
- Swapping duplicate-route order and making the synthetic route highest-scored
  changed global facts, route provenance, hypothesis identity, and confirmation
  facts.
- Progress-navigation score changes altered the hypothesis claim, semantic hash,
  supporting evidence, and recursive Judge facts.
- Direct evidence-capsule construction selected the entire highest-scored route
  and discarded authentic recorded provenance.

The GREEN regressions exercise real analyzer and confirmation flows, including a
checkpoint interruption after the first completed confirmation and resume from
that checkpoint. They also cover direct model construction, report parsing,
evaluator validation, recursive progress navigation, retrieval-global fusion,
competitor confirmation facts, and direct capsule construction.

## Verification

- Focused Task 1 state/Judge/retrieval/analyzer/checkpoint suite: `356` tests
  passed.
- Full Python attribution suite: `578` tests passed.
- `compileall` passed with `PYTHONPYCACHEPREFIX` directed to `/tmp` because the
  host default cache path is outside the workspace sandbox.
- `git diff --check` passed.

## Remaining Risk

Legacy hypotheses and confirmations without a seed binding identity retain their
existing identity calculation for backward compatibility. Modern analyzer v3
output always emits the seed binding, and modern per-seed report validation fails
closed if a listed confirmation omits or mismatches it.
