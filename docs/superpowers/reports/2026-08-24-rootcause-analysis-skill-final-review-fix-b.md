# Root-Cause Analysis Skill Final Review Fix B

Date: 2026-08-24

## Retained Outcome

This round established the durable evaluation handoff:

- all seven fixtures can be projected into opaque workspaces;
- derived Trace manifest and node scopes use the opaque case identity;
- canonical node payload/source integrity is recomputed and validated;
- declared Artifacts are copied only after containment and digest checks;
- the canonical Skill and runtime-specific prompts are included;
- hidden expected outcomes, rubric, sibling fixtures, reports, Git history, and
  symlinks are absent;
- fixture mapping and source/derived provenance remain evaluator-side.

## Superseded Scope

This round also experimented with an in-repository Agent launcher. That scope is
superseded and removed. Local working-directory separation was never a scored
trust boundary, and the repository no longer claims to provide one.

`prepare_isolated_bundle.py` is now the only evaluation helper. Scored execution
must be delegated to an independently managed trusted benchmark Harness whose
credential, filesystem, network, cleanup, runtime attestation, and audit controls
are outside this repository.

Historical local/source observations remain unscored smoke evidence only.
