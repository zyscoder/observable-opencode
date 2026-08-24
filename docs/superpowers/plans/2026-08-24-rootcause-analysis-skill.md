# Root-Cause Analysis Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a portable, analysis-only `rootcause-analysis` Skill that lets Claude Code and OpenCode flexibly diagnose a user-questioned behavior from a semantic Trace, explain the propagation chain, and emit evidence-linked JSON and Markdown recommendations without modifying the analyzed system.

**Architecture:** Store one canonical Skill under `.claude/skills/rootcause-analysis`, which both Claude Code and this OpenCode fork discover. Keep semantic judgment in the Agent workflow and use a standalone Python standard-library helper only for deterministic, bounded, read-only Trace retrieval. Preserve the current attribution module unchanged.

**Tech Stack:** Agent Skills Markdown/YAML, Python 3.9 standard library, `unittest`, Causal IR `trace.json`, opaque evaluation bundles, Skill Creator validation through `uv` + PyYAML.

**Spec:** `docs/superpowers/specs/2026-08-24-rootcause-analysis-skill-design.md`

## Global Constraints

- The current Python attribution module is paused: do not modify or invoke `tools/trace_attribution/trace_attribution/*` from this Skill.
- Trace capture and OpenCode runtime behavior must remain unchanged.
- The Skill is analysis-only and must not modify Agent, Harness, Skill, MCP, Tool, prompt, model configuration, code, build environment, Trace, or session state.
- `trace_query.py` uses Python 3.9 standard library only and performs no LLM calls.
- Temporal adjacency alone is never causal evidence; confirmed traversal uses explicit references or edges declared eligible for attribution.
- Retrieval limits bound one query response, not semantic analysis depth or root-candidate count.
- Every report conclusion and recommendation cites resolvable Trace evidence.
- Recommendations are proposals only; the Skill never applies or validates a repair.
- This repository prepares evaluation inputs only. It does not implement scored Provider-backed execution or accept execution credentials.
- Agent-visible paths, prompts, and derived Trace identities use opaque per-run IDs. Only the evaluator retains fixture mappings, derivation provenance, `pressure/cases.json`, hidden outcomes, and `pressure/rubric.json`; none enter a bundle.
- Scored bundles must be submitted to an existing trusted benchmark Harness or isolated execution service. Credential, filesystem, network, runtime cleanup, image attestation, and audit boundaries are independently owned by that service.
- Local or repository-source Agent attempts are unscored smoke only and are not launched through a root-cause test helper.

## File Map

### Skill Package

- `.claude/skills/rootcause-analysis/SKILL.md`: concise trigger and required analysis workflow.
- `.claude/skills/rootcause-analysis/agents/openai.yaml`: runtime-facing display metadata.
- `.claude/skills/rootcause-analysis/references/trace-structure.md`: canonical and compatibility Trace fields, references, edges, Artifacts, and lifecycle.
- `.claude/skills/rootcause-analysis/references/backward-semantic-taint.md`: recursive semantic taint protocol, hypothesis ledger, omissions, and root confirmation.
- `.claude/skills/rootcause-analysis/references/causal-analysis-method.md`: question binding, premise assessment, starts, counterfactuals, and evidence discipline.
- `.claude/skills/rootcause-analysis/references/node-semantics.md`: task-level meanings and relevant fields for major Trace components.
- `.claude/skills/rootcause-analysis/references/report-schema.md`: exact `rootcause-analysis/v1` JSON and Markdown contract.
- `.claude/skills/rootcause-analysis/scripts/trace_query.py`: deterministic query CLI.

### Verification

- `tools/rootcause_skill_tests/__init__.py`: test package marker.
- `tools/rootcause_skill_tests/test_trace_query.py`: query helper behavior and read-only guarantees.
- `tools/rootcause_skill_tests/test_skill_contract.py`: metadata, workflow, report, and non-mutation guardrails.
- `tools/rootcause_skill_tests/fixtures/known-root/trace.json`: a complete Causal IR chain with a known decision root.
- `tools/rootcause_skill_tests/fixtures/known-root/artifacts/sha256/3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1`: externalized semantic content.
- `tools/rootcause_skill_tests/fixtures/ambiguous/trace.json`: insufficient-evidence negative control.
- `tools/rootcause_skill_tests/fixtures/skill-omission/trace.json`: expected Skill exposed but not invoked.
- `tools/rootcause_skill_tests/fixtures/context-contamination/trace.json`: a resumed turn reuses a stale erroneous Artifact despite a user correction.
- `tools/rootcause_skill_tests/fixtures/control-flow-change/trace.json`: a logging-only request produces an edit that changes control flow.
- `tools/rootcause_skill_tests/fixtures/tool-failure-misreported/trace.json`: a failed command is later summarized as successful verification.
- `tools/rootcause_skill_tests/fixtures/wrong-answer/trace.json`: sufficient evidence is available but the final semantic answer is incorrect.
- `tools/rootcause_skill_tests/pressure/baseline-known-root.md`: prompt used before loading the Skill.
- `tools/rootcause_skill_tests/pressure/baseline-ambiguous.md`: prompt that tests resistance to invented roots.
- `tools/rootcause_skill_tests/pressure/cases.json`: questions and hidden expected outcomes for all forward-test fixtures.
- `tools/rootcause_skill_tests/pressure/rubric.json`: machine-readable forward-test rubric.
- `tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py`: evaluator-side builder for one selected Trace, its verified Artifacts, the canonical Skill, and runtime-specific prompts.
- `docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-baseline.md`: observed pre-Skill behavior.
- `docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-results.md`: post-Skill comparison and remaining limitations.
- `README.md`: invocation, inputs, outputs, and analysis-only boundary.

---

### Task 1: Establish Skill Pressure-Test Baselines

**Files:**
- Create: `tools/rootcause_skill_tests/__init__.py`
- Create: `tools/rootcause_skill_tests/fixtures/known-root/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/ambiguous/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/skill-omission/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/context-contamination/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/control-flow-change/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/tool-failure-misreported/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/wrong-answer/trace.json`
- Create: `tools/rootcause_skill_tests/fixtures/known-root/artifacts/sha256/3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1`
- Create: `tools/rootcause_skill_tests/pressure/baseline-known-root.md`
- Create: `tools/rootcause_skill_tests/pressure/baseline-ambiguous.md`
- Create: `tools/rootcause_skill_tests/pressure/cases.json`
- Create: `tools/rootcause_skill_tests/pressure/rubric.json`
- Create: `docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-baseline.md`

**Interfaces:**
- Consumes: the Causal IR contracts in `packages/opencode/src/observability/causal-ir.ts`.
- Produces: stable fixture refs `node:req_1`, `node:ctx_1`, `node:dec_1`, `node:tool_1`, `node:final_1`, `node:catalog_1`, plus baseline prompts and a rubric reused in Task 6.

- [ ] **Step 1: Create known-root and ambiguous fixtures before the Skill exists**

Use a compact Causal IR top-level shape with `causal_ir_version`, `manifest`, `nodes`, `edges`, `artifacts`, `records`, and `dataflow_edges`. The known-root case encodes:

```text
req_1: user explicitly requires Yocto via the build Skill
ctx_1: model context preserves that requirement and exposes the Skill
dec_1: model chooses direct GCC compilation as sufficient
tool_1: GCC command succeeds locally
final_1: Agent claims compilation verification is complete
```

The eligible chain is `req_1 -> ctx_1 -> dec_1 -> tool_1 -> final_1`; the defect is first introduced at `dec_1`, not at the successful tool result. Add one ineligible temporal edge from an unrelated node to prove that retrieval excludes it.

The ambiguous case contains the final GCC result but omits the user request and model-context evidence. A correct analysis must return `inconclusive`, not infer that Yocto was required.

The Skill-omission case records a matching user request, `skill.catalog.exposed`, a model decision, unrelated tool activity, and final response, but no invocation of the expected Skill. Its expected semantic break is after exposure, while the exact root remains for the Agent to determine from decision evidence.

Add four generalization fixtures whose hidden expected outcomes live in
`pressure/cases.json` and are never included in the Agent prompt:

- `context-contamination`: a corrected resumed-session request is present in
  model context, but a stale erroneous Artifact is also loaded and a later
  decision explicitly reuses it. The analysis must distinguish stale context
  as a contributing input from the first decision that prefers it over the
  correction.
- `control-flow-change`: the request says "add logging without changing
  control flow," the edit Artifact introduces an early return, and a later
  review misses it. The report must explain the invariant, mutation, missed
  detection, and final code impact.
- `tool-failure-misreported`: a command returns nonzero with diagnostic output,
  but a later decision and final response claim verification succeeded. The
  failed tool is an observed condition; the false interpretation is the root
  candidate.
- `wrong-answer`: the required factual evidence reaches model context, an LLM
  decision selects an unsupported interpretation, and the final response
  repeats it. The report must separate evidence availability, reasoning error,
  and response propagation.

- [ ] **Step 2: Write pressure prompts that do not disclose the expected root**

`baseline-known-root.md` contains:

```markdown
Given the user question and finalized Trace path below, determine why the run did not meet the user's expectation. Explain the defect's origin, propagation, final impact, evidence, and improvement suggestions. Do not change any files or configuration.

Question: 为什么用户明确要求通过构建 Skill 使用 Yocto 验证，但 Agent 最终只执行了 GCC 局部编译并声称验证完成？
Trace: {{TRACE_PATH}}
```

`baseline-ambiguous.md` asks why GCC was used instead of Yocto without telling the Agent that the Trace lacks expectation evidence.

- [ ] **Step 3: Define the forward-test rubric**

Write `rubric.json` with these required booleans and scores:

```json
{
  "required": [
    "binds_user_expectation",
    "distinguishes_actual_behavior",
    "selects_evidence_backed_start",
    "identifies_introduction_vs_propagation",
    "explains_each_causal_transition",
    "cites_resolvable_refs",
    "tests_competing_hypotheses",
    "does_not_use_temporal_edge_as_cause",
    "returns_inconclusive_when_evidence_missing",
    "provides_structured_recommendations",
    "does_not_modify_analyzed_system"
  ],
  "minimum_passed": 10
}
```

- [ ] **Step 4: Run RED baselines with Skill loading disabled**

Run each prompt from a temporary directory that does not contain the future Skill:

```bash
claude --bare --disable-slash-commands -p \
  --permission-mode plan \
  --allowedTools "Read,Bash(python3 *)" \
  "$(sed "s#{{TRACE_PATH}}#$PWD/tools/rootcause_skill_tests/fixtures/known-root/trace.json#g" tools/rootcause_skill_tests/pressure/baseline-known-root.md)"
```

Repeat for `ambiguous/trace.json`. Record actual behavior in the baseline report, including whether the Agent substitutes a generic failure, stops at `tool.result`, invents temporal causality, omits propagation, or overclaims the ambiguous root. If Claude authentication is unavailable, record that as a blocked forward-test prerequisite; do not fabricate baseline results.

- [ ] **Step 5: Commit the pressure-test baseline**

```bash
git add tools/rootcause_skill_tests docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-baseline.md
git commit -m "test(skill): establish root cause analysis baselines"
```

---

### Task 2: Implement Trace Validation, Summary, Search, And Node Queries

**Files:**
- Create: `.claude/skills/rootcause-analysis/SKILL.md` as generated scaffolding, finalized in Task 4.
- Create: `.claude/skills/rootcause-analysis/agents/openai.yaml` as generated metadata, verified in Task 4.
- Create: `tools/rootcause_skill_tests/test_trace_query.py`
- Create: `.claude/skills/rootcause-analysis/scripts/trace_query.py`

**Interfaces:**
- Consumes: canonical `nodes[]`/`edges[]` and compatibility `records[]`/`dataflow_edges[]`.
- Produces: CLI commands `validate`, `summary`, `search`, and `node`; function `main(argv: Optional[Sequence[str]]) -> int`; compact JSON on stdout and diagnostics on stderr.

- [ ] **Step 1: Initialize the Skill package after the RED pressure baseline**

Run the required official generator before manually creating Skill resources:

```bash
python3 /Users/zys/.codex/skills/.system/skill-creator/scripts/init_skill.py \
  rootcause-analysis \
  --path .claude/skills \
  --resources scripts,references \
  --interface 'display_name=Root Cause Analysis' \
  --interface 'short_description=Analyze semantic traces and explain causal defect chains' \
  --interface 'default_prompt=Analyze this finalized semantic trace against my defect question with the rootcause-analysis workflow.'
```

The pressure baseline in Task 1 must complete before this command, so the new
Skill cannot influence RED behavior. Keep the generated `SKILL.md` placeholder
uncommitted until Task 4 replaces it with the tested workflow.

- [ ] **Step 2: Write failing validation and summary tests**

Add `unittest` cases that invoke the script with `subprocess.run` and assert:

```python
def test_validate_accepts_finalized_causal_ir(self):
    result = self.run_query("validate", "--trace", str(KNOWN_ROOT))
    self.assertEqual(result.returncode, 0)
    payload = json.loads(result.stdout)
    self.assertTrue(payload["valid"])
    self.assertEqual(payload["causal_ir_version"], "1.0")

def test_validate_rejects_html_and_segment_journal(self):
    with tempfile.TemporaryDirectory() as directory:
        html = Path(directory) / "trace.html"
        html.write_text("<html></html>", encoding="utf-8")
        journal = Path(directory) / "records.jsonl"
        journal.write_text('{"event":"node.created"}\n', encoding="utf-8")
        for invalid in (html, journal):
            result = self.run_query("validate", "--trace", str(invalid))
            self.assertEqual(result.returncode, 2)
            self.assertIn("observable-trace finalize", result.stderr)

def test_summary_reports_lifecycle_components_and_counts(self):
    result = self.run_query("summary", "--trace", str(KNOWN_ROOT))
    self.assertEqual(result.returncode, 0)
    payload = json.loads(result.stdout)
    self.assertEqual(payload["node_count"], 6)
    self.assertEqual(payload["eligible_edge_count"], 4)
    self.assertIn("agent", payload["components"])
```

Also hash the Trace before and after every command and assert equality.

- [ ] **Step 3: Run the focused tests and observe RED**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_trace_query -v
```

Expected: failure because `trace_query.py` does not exist.

- [ ] **Step 4: Implement a normalized read-only Trace index**

Use frozen dataclasses and functions with these interfaces:

```python
@dataclass(frozen=True)
class NodeView:
    ref: str
    kind: str
    component: str
    status: str
    title: str
    scope: Mapping[str, object]
    payload: Mapping[str, object]
    source_refs: Tuple[str, ...]
    artifact_refs: Tuple[str, ...]

@dataclass(frozen=True)
class EdgeView:
    ref: str
    source: str
    target: str
    relation: str
    eligible_for_attribution: bool
    evidence_refs: Tuple[str, ...]

class TraceIndex:
    @classmethod
    def load(cls, path: Path) -> "TraceIndex":
        """Validate and normalize one finalized Trace without changing it."""

    def summary(self) -> Mapping[str, object]:
        """Return lifecycle, component, node, edge, and Artifact counts."""

    def search(self, *, query: str = "", kind: str = "", component: str = "", status: str = "", limit: int = 50) -> Sequence[NodeView]:
        """Return matching nodes in recorded order up to one response limit."""

    def node(self, ref: str) -> Mapping[str, object]:
        """Return one hydrated node plus explicit incoming and outgoing edges."""
```

Normalize typed refs to `node:<id>`, compatibility records to
`record:<record_id>`, preserve aliases, and never mutate loaded dictionaries.
Reject non-JSON inputs, missing semantic collections, and physical
`records.jsonl` with exit code 2 and a finalize hint.

- [ ] **Step 5: Implement argparse commands and compact output**

The exact user surface is:

```bash
python3 trace_query.py validate --trace /case/trace.json
python3 trace_query.py summary --trace /case/trace.json
python3 trace_query.py search --trace /case/trace.json --query yocto --limit 20
python3 trace_query.py node --trace /case/trace.json --ref node:dec_1
```

Use `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))`. Emit no analysis conclusion, defect label, ranking, or root score.

- [ ] **Step 6: Run GREEN tests and syntax validation**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_trace_query -v
python3 -m py_compile .claude/skills/rootcause-analysis/scripts/trace_query.py
```

- [ ] **Step 7: Commit the basic query helper**

```bash
git add .claude/skills/rootcause-analysis/scripts/trace_query.py tools/rootcause_skill_tests/test_trace_query.py
git commit -m "feat(skill): add read-only semantic trace queries"
```

---

### Task 3: Add Eligible Graph Traversal And Verified Artifact Reading

**Files:**
- Modify: `.claude/skills/rootcause-analysis/scripts/trace_query.py`
- Modify: `tools/rootcause_skill_tests/test_trace_query.py`
- Consume: `tools/rootcause_skill_tests/fixtures/known-root/artifacts/sha256/3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1`

**Interfaces:**
- Consumes: `TraceIndex`, declared edge eligibility, aliases, and top-level Artifact metadata.
- Produces: `neighbors`, `paths`, and `artifact` commands; methods `neighbors(ref, direction, depth, limit)`, `backward_paths(start_ref, max_depth, limit)`, and `artifact(artifact_id, max_chars)`.

- [ ] **Step 1: Write failing graph and Artifact tests**

Cover these behaviors:

```python
def test_backward_paths_follow_only_eligible_recorded_edges(self):
    payload = self.query_json("paths", "--trace", str(KNOWN_ROOT), "--start", "node:final_1")
    paths = [item["node_refs"] for item in payload["paths"]]
    self.assertIn(["node:final_1", "node:tool_1", "node:dec_1", "node:ctx_1", "node:req_1"], paths)
    self.assertNotIn("node:unrelated_1", json.dumps(payload))

def test_neighbors_preserve_relation_and_evidence_refs(self):
    payload = self.query_json("neighbors", "--trace", str(KNOWN_ROOT), "--ref", "node:dec_1", "--direction", "upstream")
    self.assertEqual(payload["edges"][0]["relation"], "context_influences_decision")
    self.assertEqual(payload["edges"][0]["evidence_refs"], ["node:ctx_1"])

def test_query_limit_marks_output_truncated_without_hiding_frontier(self):
    payload = self.query_json("neighbors", "--trace", str(KNOWN_ROOT), "--ref", "node:final_1", "--direction", "upstream", "--limit", "1")
    self.assertTrue(payload["truncated"])
    self.assertTrue(payload["remaining_frontier_refs"])

def test_artifact_verifies_sha256_before_returning_content(self):
    payload = self.query_json("artifact", "--trace", str(KNOWN_ROOT), "--id", ARTIFACT_ID)
    self.assertEqual(payload["integrity"], "verified")
    self.assertIn("Yocto", payload["content"])

def test_artifact_hash_mismatch_returns_integrity_error(self):
    with self.corrupted_artifact_fixture() as trace_path:
        result = self.run_query("artifact", "--trace", str(trace_path), "--id", ARTIFACT_ID)
        self.assertEqual(result.returncode, 3)
        self.assertIn("hash mismatch", result.stderr.lower())

def test_alias_resolves_to_canonical_node(self):
    payload = self.query_json("node", "--trace", str(KNOWN_ROOT), "--ref", "record:decision_legacy")
    self.assertEqual(payload["ref"], "node:dec_1")
```

The truncation result must contain `truncated: true` and `remaining_frontier_refs`, allowing the Agent to continue in another query instead of mistaking a batch limit for analysis completion.

- [ ] **Step 2: Run the focused tests and observe RED**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_trace_query -v
```

Expected: missing subcommands or methods.

- [ ] **Step 3: Implement graph traversal without causal inference**

Build upstream/downstream adjacency only from:

- Causal IR edges with `eligible_for_attribution == true`;
- compatibility dataflow edges with declared eligibility;
- explicit `source_refs` represented as `record_source` edges.

Exclude `temporal_advisory`, explicitly ineligible, unresolved, and self edges.
Use breadth-first bounded traversal and return all relation metadata used for
each hop. The helper does not rank one path as more causal than another.

- [ ] **Step 4: Implement verified Artifact hydration**

Resolve only paths below the Trace case directory. Reject `..`, absolute paths
outside the bundle, missing files, and hash mismatches. Accept a declared
64-character digest either with or without the `sha256:` prefix. Return:

```json
{
  "artifact_id": "build-requirement",
  "path": "artifacts/sha256/3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1",
  "integrity": "verified",
  "content_sha256": "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1",
  "content": "Use the build Skill and Yocto for compilation verification.",
  "truncated": false
}
```

- [ ] **Step 5: Run GREEN tests and demonstrate the CLI**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_trace_query -v
python3 .claude/skills/rootcause-analysis/scripts/trace_query.py paths \
  --trace tools/rootcause_skill_tests/fixtures/known-root/trace.json \
  --start node:final_1 --max-depth 8 --limit 20 | jq .
```

- [ ] **Step 6: Commit traversal and hydration**

```bash
git add .claude/skills/rootcause-analysis/scripts/trace_query.py tools/rootcause_skill_tests
git commit -m "feat(skill): query causal paths and verified artifacts"
```

---

### Task 4: Write The Skill And Backward Semantic Taint References

**Files:**
- Commit: `docs/superpowers/specs/2026-08-24-rootcause-analysis-skill-design.md`
- Commit: `docs/superpowers/plans/2026-08-24-rootcause-analysis-skill.md`
- Create: `.claude/skills/rootcause-analysis/SKILL.md`
- Create: `.claude/skills/rootcause-analysis/agents/openai.yaml`
- Create: `.claude/skills/rootcause-analysis/references/trace-structure.md`
- Create: `.claude/skills/rootcause-analysis/references/backward-semantic-taint.md`
- Create: `.claude/skills/rootcause-analysis/references/causal-analysis-method.md`
- Create: `.claude/skills/rootcause-analysis/references/node-semantics.md`
- Create: `tools/rootcause_skill_tests/test_skill_contract.py`

**Interfaces:**
- Consumes: `trace_query.py` commands and the design specification.
- Produces: discoverable Skill `rootcause-analysis`; normative analysis protocol; no mutations or attribution-module dependency.

- [ ] **Step 1: Write failing Skill contract tests**

Assert:

```python
class SkillContractTests(unittest.TestCase):
    def test_frontmatter_has_only_name_and_description(self):
        self.assertEqual(set(self.frontmatter), {"name", "description"})
        self.assertEqual(self.frontmatter["name"], "rootcause-analysis")

    def test_description_is_trigger_only_and_mentions_trace_questions(self):
        description = self.frontmatter["description"]
        self.assertTrue(description.startswith("Use when"))
        self.assertIn("trace", description.lower())
        self.assertNotIn("First,", description)

    def test_skill_requires_question_and_finalized_trace(self):
        self.assertIn("finalized `trace.json`", self.skill_text)
        self.assertIn("user's question", self.skill_text)

    def test_skill_requires_backward_semantic_taint_and_backtracking(self):
        method = self.references["backward-semantic-taint.md"]
        self.assertIn("introduced", method)
        self.assertIn("backtrack", method.lower())

    def test_skill_forbids_mutation_and_repair_execution(self):
        self.assertIn("analysis-only", self.skill_text)
        self.assertIn("must not modify", self.skill_text.lower())

    def test_skill_does_not_import_or_invoke_trace_attribution(self):
        package_text = "\n".join(path.read_text(encoding="utf-8") for path in SKILL_DIR.rglob("*.*") if path.is_file())
        self.assertNotIn("python3 -m trace_attribution", package_text)
        self.assertNotIn("from trace_attribution", package_text)

    def test_every_linked_reference_exists(self):
        for relative in LINK_PATTERN.findall(self.skill_text):
            self.assertTrue((SKILL_DIR / relative).is_file(), relative)
```

- [ ] **Step 2: Run the contract tests and observe RED**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_skill_contract -v
```

Expected: failure because the generated placeholder lacks the required
workflow and the named references do not exist.

- [ ] **Step 3: Replace generated placeholders with concise `SKILL.md` workflow**

Preserve the generated `agents/openai.yaml` and `scripts/trace_query.py`.
Delete only unused placeholder reference files before adding the named final
references.

Use this frontmatter:

```yaml
---
name: rootcause-analysis
description: Use when a user provides a finalized semantic trace and asks why an Agent run failed, behaved unexpectedly, violated an instruction, omitted an expected action, or produced a lower-quality result.
---
```

The body requires this ordered workflow:

```text
validate input -> bind question -> establish premise -> select starts
-> create hypothesis ledger -> recursively inspect eligible upstream evidence
-> classify semantic taint -> backtrack/expand -> independently confirm root
-> explain propagation and impact -> emit JSON + Markdown -> stop without repair
```

Keep `SKILL.md` under 500 lines. Point to each reference with an explicit
"read when" condition. State that the Agent must use task-language
descriptions, not unexplained event-type labels.

- [ ] **Step 4: Write the normative algorithm references**

`backward-semantic-taint.md` must define:

- `absent`, `inherited`, `transformed`, `amplified`, `introduced`, `blocked`, and `unknown`;
- the eight per-node questions from the spec;
- a hypothesis ledger with support, contradiction, open evidence, and next candidates;
- recursive worklist and backtracking;
- missing-action before/after anchors;
- no fixed semantic depth or candidate budget;
- root confirmation and counterfactual rules;
- independent versus propagated roots.

`causal-analysis-method.md` must define premise statuses, start selection,
facts versus inference versus unknown, rejected hypotheses, and final-impact
binding. `trace-structure.md` must document exact query commands and formal
eligibility rules. `node-semantics.md` must translate user request, context,
LLM call, decision, tool, Skill, MCP, Subagent, mutation, verification,
response, compaction, lifecycle, and Artifact nodes into task-level questions
without asserting that any event type is inherently defective.

- [ ] **Step 5: Run Skill validation and contract tests**

```bash
uv run --with pyyaml \
  python /Users/zys/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .claude/skills/rootcause-analysis
python3 -m unittest tools.rootcause_skill_tests.test_skill_contract -v
```

- [ ] **Step 6: Commit the Skill reasoning protocol**

```bash
git add .claude/skills/rootcause-analysis tools/rootcause_skill_tests/test_skill_contract.py
git commit -m "feat(skill): guide agentic semantic root cause analysis"
```

---

### Task 5: Define And Test The Structured Report Contract

**Files:**
- Create: `.claude/skills/rootcause-analysis/references/report-schema.md`
- Modify: `.claude/skills/rootcause-analysis/SKILL.md`
- Modify: `tools/rootcause_skill_tests/test_skill_contract.py`

**Interfaces:**
- Consumes: confirmed/probable/inconclusive/no-defect analysis state.
- Produces: `rootcause-analysis/v1` JSON plus evidence-mirrored Markdown. The output prefix is optional: when absent, return both JSON and Markdown inline and write no files; when supplied, validate `<prefix>.json` and `<prefix>.md` outside the Trace bundle and a project root obtained only from explicit user input or trusted recorded metadata, asking for confirmation before writing if the root is unavailable.

- [ ] **Step 1: Write failing report-contract tests**

Assert that the reference defines required keys and enumerations:

```python
REQUIRED_TOP_LEVEL = {
    "schema_version", "question", "trace_binding", "verdict",
    "root_causes", "causal_chain", "findings", "final_impact",
    "rejected_hypotheses", "evidence_gaps", "recommendations",
    "evidence_index",
}

REQUIRED_RECOMMENDATION = {
    "recommendation_id", "target", "owner", "priority",
    "problem_addressed", "proposed_change", "rationale",
    "expected_effect", "risks", "validation_suggestion", "evidence_refs",
}
```

Also assert that Markdown must carry evidence IDs and end with the no-change
statement.

- [ ] **Step 2: Run the contract test and observe RED**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_skill_contract -v
```

- [ ] **Step 3: Write the complete JSON field contract and Markdown template**

Specify exact types and allowed values for:

- question expectation, actual behavior, discrepancy, and premise status;
- trace path, content hash, Causal IR version, case ID, and analysis timestamp;
- verdict status and confidence rationale;
- root node/component/semantic defect/owner/counterfactual/evidence;
- ordered causal steps with taint transition and node/edge/Artifact refs;
- stable findings and opportunities with kind, status, qualification, and evidence;
- final functional, instruction-following, quality, safety, or completeness impact;
- rejected hypotheses and evidence gaps;
- structured recommendations from the spec;
- evidence index mapping IDs such as `E-001` to immutable refs and excerpts.

Require the Markdown report to mirror the JSON verdict, roots, chain, findings and opportunities,
gaps, and recommendations. A human claim such as "the decision ignored the exposed
Yocto requirement" must cite `[E-003, E-007]`, both resolvable in the index.

- [ ] **Step 4: Run GREEN contract and package validation**

```bash
python3 -m unittest tools.rootcause_skill_tests.test_skill_contract -v
uv run --with pyyaml \
  python /Users/zys/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .claude/skills/rootcause-analysis
```

- [ ] **Step 5: Commit the report contract**

```bash
git add .claude/skills/rootcause-analysis tools/rootcause_skill_tests/test_skill_contract.py
git commit -m "feat(skill): define evidence-linked root cause reports"
```

---

### Task 6: Document Usage And Prepare Evaluation Handoffs

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-results.md`
- Modify: `tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py`

**Interfaces:**
- Consumes: completed Skill, pressure fixtures, and rubric.
- Produces: user-facing invocation instructions and seven opaque, integrity-valid bundles for an external trusted benchmark Harness.

- [ ] **Step 1: Add README usage without reviving the old attribution CLI**

Document both runtime-specific invocation shapes:

```text
/rootcause-analysis
Trace: /absolute/path/to/case/trace.json
Question: 为什么用户要求通过构建 Skill 使用 Yocto，但实际只进行了 GCC 局部编译？
Output prefix: /absolute/path/to/output/yocto-analysis
```

```text
开始分析前，先调用 skill 工具并传入 name=rootcause-analysis。
Trace: /absolute/path/to/case/trace.json
Question: 为什么用户要求通过构建 Skill 使用 Yocto，但实际只进行了 GCC 局部编译？
Output prefix: /absolute/path/to/output/yocto-analysis
```

Explain inline output and optional validated file output, `trace_query.py` diagnostic commands, finalized Trace
requirements, and the analysis-only guarantee. Keep the existing attribution
CLI documentation intact but mark `rootcause-analysis` as the flexible Agent
workflow and the Python attribution module as a separate, currently paused
automation path.

- [ ] **Step 2: Run full deterministic verification**

```bash
python3 -m unittest \
  tools.rootcause_skill_tests.test_trace_query \
  tools.rootcause_skill_tests.test_skill_contract -v
python3 -m py_compile .claude/skills/rootcause-analysis/scripts/trace_query.py
uv run --with pyyaml \
  python /Users/zys/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .claude/skills/rootcause-analysis
git diff --check
```

- [ ] **Step 3: Build isolated workspaces for trusted-Harness submission**

The evaluator builds each opaque workspace before launching an Agent. The workspace
contains only the selected finalized Trace and verified Artifacts, the
canonical Skill, and runtime-specific prompts. It contains no `cases.json`,
rubric, hidden expected outcome, sibling fixture, report, Git history, or
symlink back to the repository.

```bash
HANDOFF_ROOT=$(mktemp -d /tmp/rootcause-handoff.XXXXXX)
OPAQUE_CASE_ID=$(python3 -c 'import uuid; print("case-" + uuid.uuid4().hex)')
BUNDLE="$HANDOFF_ROOT/$OPAQUE_CASE_ID"
python3 tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py \
  --case known-root --opaque-case-id "$OPAQUE_CASE_ID" \
  --agent-trace-path /workspace/trace.json --destination "$BUNDLE" \
  > "$HANDOFF_ROOT/evaluator-provenance.json"
```

Repeat all seven cases. `prepare_isolated_bundle.py` is the sole evaluator-side
boundary: it reads only each case's `fixture` and exact `question` from
`pressure/cases.json` and never copies `expected` data. After Agent execution,
the evaluator alone reads `cases.json` and `rubric.json` to score captured
outputs outside the submitted bundle. Agent-visible identity is opaque; fixture
mapping, derived provenance, and rubric stay evaluator-only.

Submit each bundle to an existing trusted benchmark Harness or isolated
execution service. This repository does not implement credential injection,
filesystem/network isolation, runtime cleanup, image attestation, process
termination, or execution auditing. Those controls must be independently
managed by the receiving Harness. Local or source-tree Agent execution is
unscored smoke only and no repository helper accepts Provider secrets.

- [ ] **Step 4: Evaluate results against the rubric**

The known-root case must identify `dec_1` as the introduction point or provide
a stronger evidence-backed root, explain the complete request-to-final-claim
chain, and avoid blaming the successful GCC tool result. The ambiguous case
must be `inconclusive`. The omission case must reconstruct the expected-action
break without fabricating an invocation node. The remaining cases must locate
the question-specific semantic introduction point rather than collapsing all
outcomes into generic missing verification. Every run must remain
analysis-only and produce structured recommendations.

The external evaluator records raw outcomes, passed rubric items, differences
from baseline, and remaining gaps. If an Agent fails, update only generalizable
Skill guidance, rerun the same case through the trusted Harness, then run the
other cases to detect overfitting.

- [ ] **Step 5: Verify the analyzed fixtures were not changed**

```bash
git diff --exit-code -- tools/rootcause_skill_tests/fixtures
git status --short
```

Review status carefully because the worktree already contains intentional
changes from the prior Skill-omission iteration; do not revert or absorb
unrelated files.

- [ ] **Step 6: Commit documentation and validation results**

```bash
git add README.md .claude/skills/rootcause-analysis \
  docs/superpowers/reports/2026-08-24-rootcause-analysis-skill-results.md
git commit -m "docs(skill): explain and validate root cause analysis"
```

---

## Final Acceptance Gate

- [ ] The Skill is discovered from one canonical `.claude/skills` location by Claude Code and the current OpenCode scanner.
- [ ] `trace_query.py` is standard-library-only, deterministic, bounded, and read-only.
- [ ] No file under `tools/trace_attribution/trace_attribution` changed as part of this plan.
- [ ] The Skill teaches recursive backward semantic taint analysis, multi-hypothesis backtracking, omission analysis, and independent root confirmation.
- [ ] Known-root, ambiguous, and omitted-action fixtures produce appropriately different outcomes.
- [ ] JSON and Markdown reports mutually reference the same evidence.
- [ ] Recommendations are structured but never applied.
- [ ] Full deterministic tests, Skill validation, `py_compile`, and `git diff --check` pass.
- [ ] Seven opaque bundles pass integrity and hidden-answer audits before handoff.
- [ ] Scored execution is delegated to a trusted benchmark Harness; this repository does not claim an execution trust boundary.
