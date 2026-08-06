# Revision-Bound Evidence and Merkle Checkpoint Design

## Goal

Close the remaining evidence-delivery gap for artifact-owning Causal IR nodes
and remove repeated whole-state checkpoint serialization without changing Agent
behavior or attribution semantics.

## Evidence Binding

When a case starts with a formal `subject_revision`, every Causal IR node
created by that case inherits an immutable binding in its data envelope:

- `case_id`: the active case identifier.
- `subject_revision`: the case-start revision.
- `revision_provenance_status`: `valid`.

The binding is injected only by the passive trace writer's common `node()`
path. Caller-provided values cannot override it. Cases without a configured
subject revision retain their current representation. Attribution continues to
require exact artifact ownership, hash verification, and active-revision
eligibility; stale, ambiguous, or unbound owners remain unavailable to Judges.

## Checkpoint Representation

Checkpoint APIs continue to consume and restore full logical payloads. Before
journal append, large nested immutable values are encoded as a content-addressed
Merkle DAG:

1. Child mappings and arrays are encoded first.
2. Encoded non-root values at or above the size threshold are written once to
   `checkpoint-blobs/sha256/<digest>.json`.
3. Journal payloads contain small typed blob references.
4. Restore verifies path containment, byte length, SHA-256, JSON validity, and
   recursively hydrates the original logical payload.

The top-level journal payload remains a mapping. Small values stay inline.
Repeated candidate capsules and prior investigation pages therefore reuse the
same immutable blobs instead of being copied into every state snapshot.

Blob writes happen before journal commit. A crash may leave an unreferenced
immutable blob, but cannot publish a journal reference to missing content.
Missing, modified, cyclic, malformed, or path-escaping blob references fail
closed as checkpoint corruption.

## Compatibility And Isolation

- Existing inline checkpoint records remain readable.
- Restored `CheckpointState` payloads are logically identical to inline state.
- Record hash chains continue to cover the persisted representation.
- Trace instrumentation remains passive and cannot feed data back to the Agent.
- No model prompt, tool selection, task loop, or repository operation changes.

## Verification

1. A decision rationale artifact owner receives the formal case-start binding.
2. Caller attempts to forge another subject revision cannot override it.
3. Unconfigured traces do not claim formal revision provenance.
4. Repeated large snapshot members are physically stored once and hydrate
   exactly.
5. Missing or tampered blobs are rejected.
6. Resume, transaction, crash-tail, and full attribution regressions remain
   green.
