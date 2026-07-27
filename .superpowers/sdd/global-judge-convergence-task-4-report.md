# Global Judge Convergence Task 4 Report

## Scope

This iteration improved the offline attribution path only. It does not modify
the observed Agent, benchmark execution, model prompts used by the Agent, tool
selection, MCP/skill execution, or online control flow.

The implementation addresses four failures observed in the prior sanitized
Sphinx run:

1. lifecycle and materialized-result nodes competed with authored decisions as
   Global Judge roots;
2. Global Judge repairs lacked exact per-candidate completion obligations;
3. negative-compression payload routing ignored comparison complexity;
4. a successful Global judgment did not consume all initial branches belonging
   to the same seed.

## Implemented Design

### Global-only root eligibility

Recursive traversal still permits interruption and signal nodes because they
may be contributing or amplifying factors. The new Global root-selection policy
excludes:

- `process.signal`;
- `run.interruption`;
- a materialized `change` when an active authored predecessor records its
  producer.

A sparse `change` remains eligible when no authored producer is visible.
Candidate capsules persist this graph-aware value, and graph validation
recomputes it.

### Exact candidate matrix contract

The initial Global prompt and every repair now share one deterministic contract
with:

- exact candidate refs;
- exact candidate-to-seed paths;
- exact compared-candidate refs;
- root eligibility;
- allowed causal roles;
- mechanically fixed counterfactual identity fields;
- model-decided semantic fields;
- terminal matrix completion rules.

Validation failures now identify the exact candidate and incomplete fields.
Unknown input state remains valid for a root, condition, or amplifier when the
facts do not establish the prior state and confidence remains below 1.0.

### Complexity-aware fusion gate

The payload decision records:

- trace and capsule bytes;
- capsule-to-trace expansion ratio;
- open-root candidate count;
- absolute payload budget;
- root-matrix density;
- deterministic routing reason.

Oversized negative compression bypasses Global fusion only when the open-root
matrix is also dense. A small root matrix remains eligible because the
comparison obligations are bounded even when supporting evidence is verbose.

### Seed-wide Global consumption

A successful Global judgment now terminates every initial frontier branch owned
by the same seed, rather than only the representative branch used to construct
the request. Recursive rediscovery of the same candidate + defect + seed reuses
the existing Global confirmation request and supersedes the duplicate
hypothesis instead of creating an evidence-gap blocker.

## Verification

```text
Focused regression:                         passed
Full trace-attribution suite:               1077 tests passed
Isolated-pycache compileall:                passed
git diff --check:                           passed
```

The full suite includes multi-seed Global prepass interruption, checkpoint
restore, exact report validation, duplicate confirmation handling, and
retrieval-global fallback coverage.

## Real DeepSeek Validation

The sanitized, answer-free Sphinx Causal IR fixture was sent to
`deepseek-v4-pro` through the authorized Anthropic-compatible DeepSeek endpoint.
The key was entered through non-echoing stdin and was not persisted.

Final artifact:

```text
/private/tmp/observable-opencode-task4f-deepseek-20260727/sphinx-minimal/
```

Final outcome:

| Metric | Result |
| --- | ---: |
| Analysis outcome | `confirmed_root` |
| Confirmed root | `record:decision` |
| Root recall / precision | `1.0 / 1.0` |
| Semantic root recall / precision | `1.0 / 1.0` |
| Top-1 match | `true` |
| Fabricated refs | `0` |
| Unresolved confirmed facts | `0` |
| Duplicate identities | `0` |
| Logical Judge calls | `2` |
| Physical requests | `4` |
| Global physical requests | `3` |
| Independent confirmations | `1` |
| Unresolved branches | `0` |

The valid Global matrix classified:

- `record:decision`: `root_candidate`, absent input, present output;
- `record:prompt`: `contributing_condition`, unknown input, present output;
- `record:change`: `contributing_condition`;
- `record:verification`: `outcome_evidence`;
- `record:timeout`: `unrelated`.

Independent confirmation classified `record:decision` as a confirmed
`necessary_cause`.

## Critical Residual Gap

Primary-root attribution now succeeds, but non-root factor publication is still
incomplete. The Global matrix contains a prompt condition, while the final
report publishes no contributing condition or timeout amplifier. Against the
frozen human labels:

- root metrics are perfect;
- `factor_role_precision` is `0.0`;
- human/LLM role disagreement is `0.666667`.

The timeout disagreement is semantic rather than structural: the model labels
it unrelated, while the benchmark labels it an amplifier. The prompt condition
is present in the Global assessment but is not independently confirmed and
projected into the final factor lists.

The next iteration should preserve the now-stable root path and add an
independent non-root factor confirmation/publication boundary. It should not
infer factors directly from retrieval rank or automatically promote Global
roles without confirmation.
