# Factor Role Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace root-oriented review of non-root candidates with an independent, grounded factor-role projection that can publish conditions, amplifiers, downstream materializations, unrelated candidates, or auditable unknowns without changing Agent or Trace behavior.

**Architecture:** Root-scope queue entries continue through `RootConfirmationRequest`. Non-root entries produce a blind `FactorRoleRequest`, execute a dedicated bounded Judge operation, and persist a separate `FactorRoleJudgment`; only a `necessity_status=necessary` role result may enqueue a root-verifier escalation. Report publications are reconstructed from terminal role actions, and completed checkpoints replay without provider calls.

**Tech Stack:** Python 3 standard library, immutable dataclasses, canonical JSON/SHA-256 identities, `unittest`, existing Causal IR graph and transaction journals, Anthropic-compatible DeepSeek provider.

## Global Constraints

- Analysis remains passive, offline, read-only, and unable to affect Agent execution.
- Factor requests expose no Global role, queue scope, scheduler reason, or prior verdict.
- A factor-role judgment cannot directly publish a root or co-root.
- Existing root confirmation semantics and Top-3 root bound remain authoritative.
- Existing non-root Top-3 scheduling bound remains unchanged.
- Global `outcome_evidence` joins conditions, amplifiers, and unrelated rows in
  the blind non-root Top-3 review pool; its Global label remains scheduling-only.
- Physical provider requests remain globally bounded and exactly journaled.
- Completed checkpoint replay performs zero provider calls.
- No API key or provider credential may be persisted.
- Existing uncommitted work in shared files must be preserved; do not revert or overwrite it.

---

### Task 1: Factor Role Domain And Factual Projection

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Create: `tools/trace_attribution/tests/test_factor_role_projection.py`

**Interfaces:**
- Produces: `FactorRoleJudgment`.
- Produces: `FactorRoleRequest`.
- Produces: `factor_role_request_projection(...)`.
- Produces: `factor_role_request_identity(...)`.
- Produces: exact necessity/role/mechanism/counterfactual validation.

- [ ] **Step 1: Write failing domain tests**

Add tests that instantiate:

```python
FactorRoleJudgment(
    candidate_ref="record:prompt",
    necessity_status="not_necessary",
    factor_role="contributing_condition",
    reason="The ambiguous requirement enabled the downstream omission.",
    confidence=0.83,
    evidence_refs=("record:prompt",),
    recursive_path=("record:prompt", "record:decision", "record:defect"),
    factor_mechanism={
        "schema": "factor-role-mechanism/v1",
        "mechanism_type": "enabling_condition",
        "source_ref": "record:prompt",
        "target_ref": "record:decision",
        "effect": "increased_defect_likelihood",
    },
    counterfactual={
        "schema": "factor-role-counterfactual/v1",
        "intervention_ref": "record:prompt",
        "intervention_kind": "replace_with_semantically_correct_behavior",
        "predicted_effect": "reduces_defect_likelihood",
    },
    hypothesis_id="hypothesis:test",
    hypothesis_semantic_hash="sha256:test",
    defect_fingerprint="sha256:defect",
    seed_binding_identity="seed:test",
    analysis_perspective="Improve repository reasoning.",
    request_identity="factor-role-request:v1:test",
)
```

Assert that:

- `necessary + contributing_condition` is rejected;
- `not_necessary + unknown` is rejected;
- `unknown + non-unknown role` is rejected;
- conditions and amplifiers require non-empty mechanisms;
- materialization permits only
  `predicted_effect=defect_still_present_without_materialization`;
- unrelated carries neither a mechanism nor a causal-effect claim;
- `to_dict()/from_dict()` preserves exact canonical identity.

- [ ] **Step 2: Run the domain tests and verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection -v
```

Expected: import failure because the factor-role types do not exist.

- [ ] **Step 3: Implement `FactorRoleJudgment`**

Add exact constants and a frozen dataclass to `causal_state.py`:

```python
FACTOR_NECESSITY_STATUSES = frozenset(
    {"necessary", "not_necessary", "unknown"}
)
FACTOR_ROLES = frozenset(
    {
        "contributing_condition",
        "amplifying_factor",
        "downstream_materialization",
        "unrelated",
        "unknown",
    }
)
```

The dataclass must compute a canonical `judgment_identity` from every semantic
field and reject persisted identities that differ from the recomputed value.

- [ ] **Step 4: Write failing factual-projection tests**

Construct a `FactorRoleRequest` with a candidate, path snapshots, evidence,
obligations, and confirmed-root summaries. Assert:

```python
projection = factor_role_request_projection(request)
self.assertNotIn("global_role", stable_json(projection))
self.assertNotIn("review_scope", stable_json(projection))
self.assertNotIn("origin", stable_json(projection))
self.assertEqual(
    factor_role_request_projection_identity(projection),
    factor_role_request_identity(request),
)
```

Mutate each factual field individually and assert identity changes. Add unknown
and extra fields and assert exact-schema rejection.

- [ ] **Step 5: Implement immutable request and projection**

Mirror the root request's frozen snapshot and exact projection pattern, with
these factual keys:

```python
{
    "candidate_ref",
    "defect_state",
    "recursive_path",
    "candidate_reference",
    "recursive_path_references",
    "supporting_evidence",
    "opposing_evidence",
    "task_obligations",
    "confirmed_root_summaries",
    "hypothesis_id",
    "hypothesis_semantic_hash",
    "seed_binding_identity",
    "analysis_perspective",
}
```

Use schema `factor-role-request-projection/v1` and identity prefix
`factor-role-request:v1:`.

- [ ] **Step 6: Run focused state and Judge tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_state \
  tools.trace_attribution.tests.test_causal_judge -v
```

Expected: all tests pass.

### Task 2: Independent Factor Judge And Provider Boundary

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Modify: `tools/trace_attribution/tests/test_causal_judge.py`
- Modify: `tools/trace_attribution/tests/test_factor_role_projection.py`

**Interfaces:**
- Produces: `build_factor_role_prompt(request: FactorRoleRequest) -> str`.
- Produces: `parse_factor_role_judgment(...) -> FactorRoleJudgment`.
- Produces: `judge_factor_role_bounded(...)`.
- Produces: `judge_factor_role_offline(...)`.

- [ ] **Step 1: Write failing prompt-boundary tests**

Assert the prompt:

- contains every request fact;
- contains no Global role or scheduling metadata;
- describes necessity and role as independent dimensions;
- distinguishes a causal factor from a downstream materialization;
- requires exact evidence refs and request identity;
- never instructs the model to match an expected benchmark label.

- [ ] **Step 2: Write failing parser-grounding tests**

Feed valid JSON for each role and then mutate:

- candidate ref;
- request identity;
- hypothesis/defect/seed binding;
- evidence ref outside the request;
- recursive path;
- mechanism source or target;
- necessity/role combination;
- counterfactual effect.

Each mutation must fail with a specific grounding or consistency error.

- [ ] **Step 3: Run focused tests and verify RED**

Expected: missing prompt builder, parser, and bounded provider method.

- [ ] **Step 4: Implement prompt and parser**

Add `FACTOR_ROLE_SYSTEM_PROMPT` and an exact required JSON schema. The prompt
must explicitly require:

```json
{
  "necessity_status": "necessary|not_necessary|unknown",
  "factor_role": "contributing_condition|amplifying_factor|downstream_materialization|unrelated|unknown"
}
```

Parse first, validate against request facts second, then construct the immutable
judgment. Provider repair receives the same request facts and exact validation
error.

- [ ] **Step 5: Extend Judge capabilities**

Add methods to `CausalJudge`, `BoundedJudgeCapability`,
`OfflineJudgeCapability`, and `OfflineCausalJudgeAdapter`:

```python
def judge_factor_role(
    self, request: FactorRoleRequest
) -> FactorRoleJudgment: ...

def judge_factor_role_bounded(
    self,
    request: FactorRoleRequest,
    *,
    max_physical_requests: Optional[int],
) -> BoundedJudgeCallResult: ...
```

Legacy offline adapters may derive an unknown role only when the wrapped Judge
does not implement the factor method; they must not silently route the request
through `confirm_candidate`.

- [ ] **Step 6: Implement Claude/DeepSeek-compatible execution and cache**

Use the existing `_request_with_repair` transport and cache behavior with a
factor-specific operation/cache identity. Verify:

- valid cached completion uses zero physical requests;
- one malformed response plus repair uses two requests;
- an exhausted allowance fails with exact physical count;
- the provider circuit and timeout behavior match existing Judge operations.

- [ ] **Step 7: Run Judge regressions**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_judge -v
```

Expected: all tests pass.

### Task 3: Non-Root Action Lifecycle And Replay

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/checkpoint.py`
- Modify: `tools/trace_attribution/tests/test_factor_role_projection.py`
- Modify: `tools/trace_attribution/tests/test_causal_checkpoint.py`

**Interfaces:**
- Produces: `factor_role_started|factor_role_completed|factor_role_failed`.
- Produces: exact factor request/response action projections.
- Produces: persisted `factor_role_judgments`.
- Preserves: root-scope `confirmation_*` actions.

- [ ] **Step 1: Write failing lifecycle dispatch tests**

Queue one root and one non-root entry. Assert:

- root invokes only `confirm_candidate_bounded`;
- non-root invokes only `judge_factor_role_bounded`;
- both consume the same global physical request budget exactly;
- terminal queue entries bind to the corresponding action identity;
- provider failure creates a terminal unknown factor judgment and a factor gap.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Expected: non-root currently invokes `confirm_candidate_bounded`.

- [ ] **Step 3: Define factor action projections**

Add exact started and terminal projections containing:

```python
{
    "owner",
    "origin",
    "candidate_ref",
    "hypothesis_id",
    "defect_fingerprint",
    "seed_binding_identity",
    "request_projection",
    "request_identity",
    "physical_requests_reserved",
}
```

Terminal actions additionally contain exact request delta, judgment payload,
judgment identity, physical request delta, and failure classification.

- [ ] **Step 4: Route non-root queue entries through the factor operation**

Factor request construction must reuse the same resolved candidate/path/evidence
snapshots as root confirmation while replacing competitor hypotheses with
canonical confirmed-root summaries. It must not copy the Global assessment role
into any request field.

Extend the reviewable Global non-root pool to include `outcome_evidence` under
the same Top-3 bound. Ranking and enqueue provenance may retain the scheduler's
source row outside the request, but the role label must remain absent from
Judge-visible facts.

- [ ] **Step 5: Write failing replay and corruption tests**

Persist a completed factor action, resume with a Judge that raises on every
call, and assert:

- zero provider requests;
- identical role judgment and publication;
- identical request and response identities.

Then forge request facts, response role, queue owner, action origin, or physical
counts and assert checkpoint restore fails closed.

- [ ] **Step 6: Implement restore, replay, and schema versioning**

Version:

- action state from v19 to v20;
- checkpoint from v21 to v22;
- output transaction from v10 to v11.

Add `factor_role_contract` to checkpoint compatibility configuration so a
provider-free replay cannot load artifacts produced under a different role
truth model.

- [ ] **Step 7: Run lifecycle and checkpoint regressions**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_checkpoint \
  tools.trace_attribution.tests.test_causal_role_closure -v
```

Expected: all tests pass.

### Task 4: Canonical Role Publications And Report Closure

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/tests/test_factor_role_projection.py`
- Modify: `tools/trace_attribution/tests/test_causal_state.py`

**Interfaces:**
- Produces: `CausalMaterialization`.
- Produces: `canonical_factor_role_publication(...)`.
- Produces: report collection `downstream_materializations`.
- Produces: report schema v22.

- [ ] **Step 1: Write failing publication tests**

From completed factor judgments assert:

- condition -> `contributing_conditions`;
- amplifier -> `amplifying_factors`;
- materialization -> `downstream_materializations`;
- unrelated -> `rejected_candidates`;
- unknown -> `factor_confirmation_gaps`;
- no factor judgment appears in `confirmations`;
- no role identity appears in two publication collections.

- [ ] **Step 2: Run publication tests and verify RED**

Expected: reports know only `RootConfirmation`-backed factor publications and
have no materialization collection.

- [ ] **Step 3: Add canonical factor and materialization projections**

`CausalFactor.confirmation` becomes a role-judgment projection for new reports.
Add `CausalMaterialization` with candidate, path, evidence, reason, confidence,
role judgment, provenance, and materialization mechanism.

Keep the canonical publication functions strict: they reconstruct all public
fields from the terminal role judgment and reject caller-supplied divergence.

- [ ] **Step 4: Extend report serialization and validation**

Version the report to `recursive-attribution-report/v22`. Validate:

- each publication maps to exactly one completed non-root role action;
- every request/response identity matches its journal;
- necessity and role are consistent;
- evidence, path, and mechanism are request-grounded;
- no necessary signal appears as a direct root;
- no root `RootConfirmation` is replaced by a factor judgment.

- [ ] **Step 5: Add coordinated-forgery tests**

Simultaneously edit queue, action, judgment, and report fields while retaining
internally plausible values. Assert validation still rejects:

- relabeling a materialization as a condition;
- converting unrelated to amplifier;
- publishing a necessary signal as a co-root;
- replacing grounded evidence with an unoffered ref.

- [ ] **Step 6: Run report closure tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_state \
  tools.trace_attribution.tests.test_causal_role_closure -v
```

Expected: all tests pass.

### Task 5: Necessary-Cause Escalation Without Direct Promotion

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/tests/test_factor_role_projection.py`

**Interfaces:**
- Produces: `factor_role_escalation` origin.
- Consumes: completed `FactorRoleJudgment(necessity_status="necessary")`.
- Preserves: existing root/co-root reciprocal confirmation rules.

- [ ] **Step 1: Write failing escalation tests**

Assert:

1. A necessary factor judgment does not modify roots.
2. It enqueues one root-scope confirmation bound to the factor action.
3. Replaying the same factor action cannot enqueue a duplicate.
4. A confirmed root result follows existing primary/co-root ranking.
5. A rejected or unknown root result is preserved as an escalation gap and
   leaves existing roots unchanged.
6. The escalation does not consume the ordinary Global root Top-3 queue quota
   already closed for that seed, but at most one escalation per non-root
   candidate is allowed.

- [ ] **Step 2: Run escalation tests and verify RED**

Expected: factor judgments and escalation origin do not exist.

- [ ] **Step 3: Implement escalation ownership**

The escalation origin must bind:

```python
{
    "kind": "factor_role_escalation",
    "factor_action_identity": "...",
    "factor_judgment_identity": "...",
    "factor_request_identity": "...",
}
```

Root request facts are rebuilt from authoritative state; they are not copied
from provider output. Existing mutual co-root and independent confirmation
checks remain unchanged.

- [ ] **Step 4: Validate escalation closure**

Report validation must reject an escalation whose factor judgment is not
necessary, is not completed, belongs to another seed/hypothesis/defect, or has
already been consumed by a different root action.

- [ ] **Step 5: Run root and role regressions**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_root_confirmation_fix39 \
  tools.trace_attribution.tests.test_seed_attribution -v
```

Expected: all tests pass.

### Task 6: Full Regression And Real Benchmark Calibration

**Files:**
- Create: `docs/superpowers/reports/2026-07-27-factor-role-projection-results.md`

**Interfaces:**
- Consumes: completed implementation and sanitized open-source benchmark traces.
- Produces: root and factor metrics, cost/replay measurements, disagreement
  analysis, and the next iteration decision.

- [ ] **Step 1: Run the complete attribution suite**

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
```

Expected: all tests pass.

- [ ] **Step 2: Run compilation and diff checks**

```bash
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache \
  python3 -m compileall -q tools/trace_attribution
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Run sanitized Sphinx regression**

Use the authorized Anthropic-compatible DeepSeek endpoint through environment
variables without echoing or persisting credentials. Record:

- root Top-1, recall, and precision;
- factor-role precision and unknown rate;
- Global-versus-independent disagreement;
- physical and logical Judge request counts;
- serialized trace/report size;
- completed-checkpoint replay provider count;
- fabricated evidence count.

- [ ] **Step 4: Run at least two distinct open-source benchmark traces**

Select cases that exercise different failure shapes:

- ambiguous requirement or missing private-domain constraint;
- correct planning with incorrect implementation/tool materialization;
- timeout or harness interruption that amplifies but does not introduce the
  defect.

Compare manual backward semantic-taint analysis with the offline module's
candidate set, root decision, and factor roles.

- [ ] **Step 5: Verify regression and target deltas**

At minimum, verify:

- the known Sphinx decision root remains a root;
- prompt/context is not promoted solely because it precedes the root;
- implementation/change can be represented as downstream materialization;
- timeout can be represented as an amplifier when evidence supports it;
- completed replay performs zero provider requests;
- fabricated evidence references remain zero.

- [ ] **Step 6: Write the results report**

Save exact artifact paths, hashes, metrics, disagreements, failure analysis,
and the recommended next phase to
`docs/superpowers/reports/2026-07-27-factor-role-projection-results.md`.

Do not claim the quantitative targets are met unless measured across the
specified benchmark traces.
