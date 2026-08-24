# Root-Cause Analysis Skill Strategic Scope Correction

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Decision

This repository now owns only deterministic preparation of root-cause Skill
evaluation inputs. The experimental in-repository Agent execution path is removed.
The repository does not implement scored Provider-backed execution, credential
handling, execution isolation, network policy, runtime cleanup, image attestation,
process supervision, or execution audit security.

This is an ownership correction, not a relaxation of scored evaluation security.
Those controls belong in an existing trusted benchmark Harness or isolated
execution service that is independently operated and reviewed.

## Retained Handoff

`prepare_isolated_bundle.py` remains the sole evaluation helper. For each of the
seven fixture cases it:

1. validates the finalized source Trace and canonical node integrity;
2. verifies every declared Artifact digest and containment boundary;
3. creates a no-overwrite workspace with a fresh opaque case identity;
4. sanitizes manifest and scope identities and recomputes derived integrity;
5. includes the canonical `rootcause-analysis` Skill and runtime-specific prompts;
6. preserves the exact user question while excluding hidden expected outcomes;
7. emits fixture mapping, source digest, derived digest, and derivation provenance
   to evaluator-side stdout rather than into the Agent-visible bundle.

Every canonical node must provide a valid source hash. The Harness-visible Trace
path embedded in prompts must be an absolute POSIX path without control characters
or parent traversal.

The bundle contains no case catalog, rubric, sibling fixture, report, Git history,
semantic fixture identity, or symlink back to the repository.

## Handoff Contract

The evaluator submits exactly one opaque bundle to a trusted benchmark Harness.
The Harness independently owns credentials, filesystem and network isolation,
runtime cleanup, execution environment attestation, termination, and audit
storage. It returns model outputs to an evaluator that alone can access the
fixture-to-opaque mapping and hidden rubric.

Local or source-tree Agent attempts remain unscored smoke only. This repository
ships no root-cause helper that launches an Agent or accepts Provider credentials.

## Historical Records

Earlier reports described successive attempts to construct an execution trust
boundary in this repository. Those sections are superseded. Historical Claude
authentication and OpenCode provider-discovery failures remain factual unscored
observations, not evidence of Skill quality or safe scored execution.

## Verification Intent

The acceptance gate now covers only repository-owned guarantees: deterministic
tests, Python compilation for the builder and query helper, official Skill
validation, seven fresh opaque bundle builds, hidden-answer and symlink audits,
fixture immutability, and clean diffs. Actual model scoring is deliberately
outside this repository and must not be inferred from these checks.

## Verified Result

- 102 deterministic tests passed with no skips.
- `trace_query.py` and `prepare_isolated_bundle.py` compiled successfully.
- The official Skill validator reported `Skill is valid!`.
- Seven fresh bundles were built under
  `/tmp/rootcause-strategic-handoff-r4ig_ieq` with opaque identities, zero
  hidden-answer leaks, zero symlinks, and evaluator provenance outside every
  Agent workspace.
- The canonical fixtures were unchanged and repository diff checks passed.

No Agent was launched and no scored result is claimed by this verification.
