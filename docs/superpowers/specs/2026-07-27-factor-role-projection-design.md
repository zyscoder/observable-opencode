# Factor Role Projection Design

## 1. Goal

Separate independent non-root causal-role assessment from necessary-root
confirmation in the offline attribution module.

The change must improve factor-role precision without weakening root recall,
grounding, checkpoint replay, or the passive-analysis boundary. It must not
modify Agent execution, Trace collection, prompts sent by the Agent, or any
runtime decision visible to the Agent.

## 2. Current Problem

`RootConfirmation` currently represents two different questions:

1. Is this candidate a necessary root cause?
2. If it is not a necessary root, is it a contributing condition, an
   amplifying factor, unrelated, or unknown?

The response schema ties `status=rejected` to non-root roles. This creates
three avoidable ambiguities:

- rejecting necessity can be mistaken for rejecting causal relevance;
- non-root role evidence is forced into root-oriented counterfactual and
  competitor fields;
- one provider response can accidentally promote a factor into a root even
  though it was not independently reviewed under the root contract.

The existing Causal Role Closure phase made non-root findings auditable, but
the truth model still couples necessity and role.

## 3. Options

### Option A: Independent Factor Role Projection

Add a dedicated factual request and judgment for non-root candidates. Keep the
existing root confirmation path unchanged. Escalate a factor candidate to the
root verifier only when the independent role judgment identifies credible
necessary-cause evidence.

This is the selected design. It separates semantics, normally costs one
provider request per factor, and preserves the existing mature root-confirmation
contract.

### Option B: Mandatory Two-Pass Review

Run root confirmation and factor-role classification for every non-root
candidate.

This provides maximum procedural separation, but nearly doubles provider cost
even when the candidate is clearly non-necessary.

### Option C: Extend RootConfirmation

Add more independent fields to `RootConfirmation` while retaining one request
and lifecycle.

This minimizes code movement but retains the status/role coupling that this
phase is intended to remove.

## 4. Domain Model

### 4.1 FactorRoleRequest

`FactorRoleRequest` is an immutable snapshot containing only Judge-visible
facts:

- candidate reference and candidate semantic snapshot;
- active defect state;
- recursive path and semantic snapshots along that path;
- supporting and opposing evidence;
- task obligations;
- already confirmed root summaries for the same seed;
- hypothesis identity, semantic hash, seed binding, and analysis perspective.

The request must not expose:

- the Global Judge's causal-role label, confidence, or reason;
- queue `review_scope` or scheduler origin;
- retrieval rank as causal evidence;
- a previous root or factor verdict for the same request identity.

The exact factual payload is persisted as a versioned
`FactorRoleRequestProjection`. Its identity is a canonical hash of all and only
the Judge-visible facts.

### 4.2 FactorRoleJudgment

The judgment separates necessity from non-root role:

- `necessity_status`:
  - `necessary`;
  - `not_necessary`;
  - `unknown`.
- `factor_role`:
  - `contributing_condition`;
  - `amplifying_factor`;
  - `downstream_materialization`;
  - `unrelated`;
  - `unknown`.

It also records:

- reason and confidence;
- grounded evidence references;
- recursive path;
- a role-specific mechanism;
- a role-specific counterfactual;
- candidate, hypothesis, defect, seed, and request identities.

The dimensions are constrained as follows:

- `necessity_status=necessary` requires `factor_role=unknown`; the result is an
  escalation signal, not a published root fact.
- `necessity_status=not_necessary` permits the four definitive non-root roles.
- `necessity_status=unknown` requires `factor_role=unknown`.
- contributing and amplifying roles require a grounded mechanism and a
  counterfactual that reduces defect probability, exposure, or severity
  without claiming prevention.
- downstream materialization requires evidence that the candidate executes,
  stores, exposes, or reports an already introduced defect.
- unrelated requires evidence that no grounded causal influence is
  established and carries no factor mechanism.

### 4.3 Published Roles

Definitive judgments publish canonically:

- `contributing_condition` -> `contributing_conditions`;
- `amplifying_factor` -> `amplifying_factors`;
- `downstream_materialization` -> `downstream_materializations`;
- `unrelated` -> `rejected_candidates`;
- `unknown` -> `factor_confirmation_gaps`.

The new `downstream_materializations` collection prevents tools, file changes,
test failures, and output records from being mislabeled as causal conditions
merely because they carry the defect downstream.

## 5. Processing Flow

For each bounded Global non-root candidate:

- the review pool includes Global `contributing_condition`,
  `amplifying_factor`, `outcome_evidence`, and `unrelated` rows;
- all four Global roles are scheduling hints only and remain absent from the
  factor request;
- they share the existing non-root Top-3 quota, ranked deterministically by
  authored semantic relevance, confidence, grounded path length, and candidate
  reference;
- `outcome_evidence` is included because the independent Judge must be able to
  distinguish downstream materialization from causal contribution.

1. The scheduler creates the existing non-root queue owner and origin record.
2. The analyzer builds a blind `FactorRoleRequestProjection`.
3. The provider performs a bounded role judgment.
4. The parser verifies exact schema, request identity, evidence membership,
   path membership, counterfactual consistency, and mechanism consistency.
5. The terminal action persists both factual request and response identities.
6. A definitive non-root role is published.
7. An unknown result creates a non-blocking factor gap.
8. A `necessary` result is enqueued into the existing root-confirmation path.
9. Only a successful existing `RootConfirmation` may publish or modify the
   root/co-root set.

The Global role remains a scheduling hint and an evaluation comparison target;
it never becomes a Judge-visible fact or final causal conclusion.

## 6. Provider Contract

The provider gains one independent operation:

```python
judge_factor_role_bounded(
    request: FactorRoleRequest,
    *,
    max_physical_requests: Optional[int],
) -> BoundedJudgeCallResult[FactorRoleJudgment]
```

The Anthropic-compatible implementation uses a factor-specific system prompt
and exact JSON schema. Repair calls receive only the factual request,
validation error, and invalid response. Repair cannot introduce new evidence
references or change the request identity.

Provider timeout, malformed output, budget exhaustion, or grounding failure
produces an explicit unknown judgment and an auditable terminal action. It does
not affect an already closed root result.

## 7. Lifecycle And Persistence

Factor review receives a separate action kind and lifecycle:

- `factor_role_started`;
- `factor_role_completed`;
- `factor_role_failed`.

Each action records:

- queue owner and scheduler origin;
- request projection and request identity;
- physical request count;
- terminal judgment and response identity;
- failure classification when applicable.

Checkpoint restore validates exact schemas and identities before accepting a
completed action. A completed provider response is replayed without another
provider call. Missing, inconsistent, or older factor-role artifacts fail
closed and cannot be interpreted as a definitive role.

Root confirmation actions remain unchanged except for accepting an escalation
origin that binds back to one completed factor-role action.

## 8. Report Validation

The report validator reconstructs expected publications from terminal
factor-role actions and rejects:

- a role publication without a completed factor judgment;
- a role inconsistent with necessity status;
- a factor whose evidence or path is absent from the request projection;
- a downstream materialization duplicated as a condition or amplifier;
- a necessary-cause claim published directly as a root;
- an escalation without a matching factor judgment;
- coordinated edits to queue, action, journal, and publication identities.

Factor-role gaps, enqueue gaps, and escalation failures remain separately
auditable.

## 9. Compatibility

This phase intentionally versions the report, checkpoint, action projection,
and output transaction schemas. Older persisted attribution artifacts fail
closed instead of being silently reinterpreted.

Compatibility is behavioral rather than byte-level:

- Agent and Trace behavior are unchanged;
- existing root confirmation semantics are preserved;
- existing factor collections retain their meaning;
- provider-free replay remains supported for artifacts created under the new
  schema.

## 10. Testing

Focused tests must prove:

1. Factor requests do not contain Global role or queue scope.
2. Rejected necessity no longer means unrelated.
3. Conditions, amplifiers, materializations, unrelated candidates, and
   unknowns publish to distinct locations.
4. A necessary result cannot publish a root directly.
5. Necessary results escalate once and use the existing root verifier.
6. Factor and root provider budgets remain independently bounded.
7. Request and response identities detect factual and coordinated forgery.
8. Checkpoint replay performs no provider call for completed role actions.
9. Provider failure creates a non-blocking factor gap.
10. Existing root-confirmation tests remain green.
11. The full attribution suite and compilation checks pass.
12. A real Sphinx regression retains the known root while evaluating prompt,
    change, and timeout with the independent role contract.

## 11. Evaluation Targets

Across Sphinx and additional open-source benchmark traces:

- root Top-1 accuracy: at least 85%;
- root candidate recall: at least 90%;
- root precision: at least 85%;
- factor-role precision: at least 70%;
- factor unknown rate: at most 20%;
- Global-versus-independent role disagreement: at most 25%;
- fabricated evidence references: 0;
- completed-checkpoint replay provider requests: 0.

The targets guide iterative validation and are not encoded as rules that bias
the Judge toward expected benchmark labels.
