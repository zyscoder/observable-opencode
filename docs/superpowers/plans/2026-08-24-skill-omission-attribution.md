# Skill Omission Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trace the passive Skill catalog lifecycle and attribute an applicable-but-missing Skill invocation from a user question.

**Architecture:** Observable OpenCode records immutable catalog and exposure facts without changing runtime selection. The Python attribution layer builds a bounded expected-action projection, verifies the omission premise, and restricts recursive retrieval to the relevant Skill lifecycle before considering generic Agent decisions.

**Tech Stack:** TypeScript, Effect, Bun test, Python 3, unittest, Causal IR v6.

**Spec:** `docs/superpowers/specs/2026-08-24-skill-omission-attribution-design.md`

## Global Constraints

- Runtime instrumentation must not change Skill discovery, precedence, permissions, prompts, tools, or Agent behavior.
- Offline attribution remains read-only and cannot feed results back into OpenCode.
- Existing Trace fields, CLI arguments, and reports remain compatible.
- Tests must cover positive, negative, and evidence-insufficient outcomes.

---

### Task 1: Skill Catalog Audit Projection

**Files:**
- Create: `packages/opencode/src/skill/catalog-observability.ts`
- Modify: `packages/opencode/src/skill/index.ts`
- Test: `packages/opencode/test/skill/catalog-observability.test.ts`

**Interfaces:**
- Produces: `SkillCatalogCandidate`, `SkillCatalogSnapshot`, `classifySkillLocation()`, and `catalogSnapshot()`.
- Consumes: parsed Skill metadata and the final existing `state.skills` map.

- [x] Write tests proving source classification, stable candidate ordering, parse-failure retention, same-name conflict projection, and preservation of the existing winner.
- [x] Run `bun --cwd packages/opencode test test/skill/catalog-observability.test.ts` and verify the tests fail because the module does not exist.
- [x] Implement the immutable audit projection and integrate candidate collection into discovery without changing the assignment to `state.skills[name]`.
- [x] Re-run the focused test and existing `test/skill/skill.test.ts` suite.

### Task 2: Session-Bound Skill Catalog Exposure

**Files:**
- Modify: `packages/opencode/src/session/system.ts`
- Modify: `packages/opencode/src/session/prompt.ts`
- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Test: `packages/opencode/test/session/system.test.ts`

**Interfaces:**
- Consumes: `SkillCatalogSnapshot` from Task 1 and model-request identities from `SessionPrompt`.
- Produces: `skill.catalog.exposed` nodes with candidate, selected, permission, exposed, and tool-availability facts.

- [x] Add a failing integration test that emits a model request with an exposed migration Skill and asserts the materialized Causal IR fields and provenance.
- [x] Run the focused Bun test and confirm `skill.catalog.exposed` is absent from the formal terminal projection.
- [x] Pass optional trace context into `SystemPrompt.skills`, emit the passive node, and map it into Causal IR without adding prompt text.
- [x] Re-run the focused observability and Skill suites and verify the trace-disabled prompt path does not read the audit catalog.

### Task 3: Expected Skill Action Projection

**Files:**
- Create: `tools/trace_attribution/trace_attribution/expected_action.py`
- Modify: `tools/trace_attribution/trace_attribution/service.py`
- Test: `tools/trace_attribution/tests/test_skill_omission_attribution.py`

**Interfaces:**
- Produces: `build_expected_action_projection(graph, question)` and `skill_question_evidence_refs(graph, question)`.
- Consumes: user messages, context transforms, `skill.catalog.exposed`, `skill.load`, and Skill tool records.

- [x] Write failing tests for exposed-and-invoked, exposed-and-omitted, missing-candidate, permission-filtered, and unrelated-question projections.
- [x] Run the focused unittest and verify failure due to the missing projection module.
- [x] Implement deterministic evidence collection, artifact hydration, bounded execution windows, and projection binding to the offline question seed.
- [x] Re-run the focused test and existing question-premise tests.

### Task 4: Skill-Scoped Premise and Retrieval

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/service.py`
- Modify: `tools/trace_attribution/trace_attribution/expected_action.py`
- Test: `tools/trace_attribution/tests/test_skill_omission_attribution.py`

**Interfaces:**
- Consumes: `expected_action_projection/v1` from Task 3.
- Produces: a localized `action_omission` premise and Skill-first predecessor candidates.

- [x] Add failing tests proving Skill lifecycle facts and final execution evidence survive unrelated calls and premise-unknown analysis does not expand generic candidates.
- [x] Run the focused unittest and verify the current generic evidence selection fails the assertions.
- [x] Add bounded Skill-specific premise evidence and post-exposure execution-window refs while retaining generic fallback for non-Skill questions.
- [x] Re-run focused tests plus `test_recursive_cli.py` and request/service regressions.

### Task 5: Outcome-Consistent Explanation and End-to-End Regression

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/defect_explanation.py`
- Test: `tools/trace_attribution/tests/test_defect_explanation.py`
- Test: `tools/trace_attribution/tests/test_skill_omission_attribution.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: structured premise, expected-action projection, and recursive outcome.
- Produces: matching JSON/Markdown conclusions and user-facing remediation ownership.

- [x] Add failing tests that `inconclusive` and `no_defect` reports never claim a first deviation and expose the Skill lifecycle projection in the final report.
- [x] Run the focused explanation tests and confirm the current generic template fails.
- [x] Implement outcome-specific explanation rendering and document the Skill-omission `--question` workflow without overwriting existing README edits.
- [x] Run focused TypeScript and Python suites, materialize a synthetic migration-skill Trace, and verify the premise Judge receives the lifecycle and execution-window evidence.
