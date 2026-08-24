import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "rootcause-analysis"
SKILL_PATH = SKILL_DIR / "SKILL.md"
OPENAI_AGENT_PATH = SKILL_DIR / "agents" / "openai.yaml"
QUERY_SCRIPT = SKILL_DIR / "scripts" / "trace_query.py"
RAW_FORWARD_REPORT_PATH = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "reports"
    / "2026-08-24-rootcause-analysis-skill-raw-attempts.md"
)
DESIGN_PATH = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-08-24-rootcause-analysis-skill-design.md"
)
PLAN_PATH = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-08-24-rootcause-analysis-skill.md"
)
REFERENCE_NAMES = {
    "backward-semantic-taint.md",
    "causal-analysis-method.md",
    "node-semantics.md",
    "report-schema.md",
    "trace-structure.md",
}
LINK_PATTERN = re.compile(r"\[[^\]]+\]\((references/[^)#]+\.md)(?:#[^)]+)?\)")
REQUIRED_TOP_LEVEL = {
    "schema_version",
    "question",
    "trace_binding",
    "verdict",
    "root_causes",
    "causal_chain",
    "findings",
    "final_impact",
    "rejected_hypotheses",
    "evidence_gaps",
    "recommendations",
    "evidence_index",
}
REQUIRED_RECOMMENDATION = {
    "recommendation_id",
    "target",
    "owner",
    "priority",
    "problem_addressed",
    "proposed_change",
    "rationale",
    "expected_effect",
    "risks",
    "validation_suggestion",
    "evidence_refs",
}
REQUIRED_OBJECT_KEYS = {
    "question": {
        "original", "expected_behavior", "actual_behavior", "discrepancy",
        "stated_final_impact", "expectation_premise", "observation_premise",
    },
    "trace_binding": {
        "trace_path", "trace_content_sha256", "causal_ir_version", "case_id",
        "analyzed_at",
    },
    "verdict": {
        "status", "summary", "confidence", "confidence_rationale", "evidence_refs",
    },
    "root_causes": {
        "root_id", "status", "node_ref", "component", "owner", "semantic_defect",
        "taint_state", "counterfactual", "evidence_refs",
    },
    "causal_chain": {
        "step_id", "step", "from_ref", "to_ref", "component", "semantic_event",
        "taint_state", "taint_transition", "explanation", "node_refs", "edge_refs",
        "artifact_refs", "evidence_refs",
    },
    "findings": {
        "finding_id", "kind", "status", "description", "qualification",
        "evidence_refs",
    },
    "final_impact": {
        "category", "status", "description", "direct_effect", "inferred_risks",
        "unknowns", "evidence_refs",
    },
    "rejected_hypotheses": {
        "hypothesis_id", "hypothesis", "disposition", "why_plausible",
        "rejection_reason", "evidence_refs",
    },
    "evidence_gaps": {
        "gap_id", "description", "why_it_matters", "needed_evidence",
        "affected_claims", "related_refs", "evidence_refs",
    },
    "evidence_index": {
        "evidence_id", "evidence_kind", "immutable_ref", "trace_ref",
        "content_sha256", "excerpt", "interpretation",
    },
}


def parse_frontmatter(text: str):
    if not text.startswith("---\n"):
        return {}
    block = text.split("---\n", 2)[1]
    values = {}
    for line in block.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def collect_evidence_refs(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_refs":
                found.extend(child)
            else:
                found.extend(collect_evidence_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(collect_evidence_refs(child))
    return found


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_text = SKILL_PATH.read_text(encoding="utf-8")
        cls.readme_text = README_PATH.read_text(encoding="utf-8")
        cls.openai_agent_text = OPENAI_AGENT_PATH.read_text(encoding="utf-8")
        cls.raw_forward_report_text = RAW_FORWARD_REPORT_PATH.read_text(encoding="utf-8")
        cls.design_text = DESIGN_PATH.read_text(encoding="utf-8")
        cls.plan_text = PLAN_PATH.read_text(encoding="utf-8")
        cls.frontmatter = parse_frontmatter(cls.skill_text)
        cls.references = {
            name: (SKILL_DIR / "references" / name).read_text(encoding="utf-8")
            if (SKILL_DIR / "references" / name).is_file()
            else ""
            for name in REFERENCE_NAMES
        }

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

    def test_runtime_facing_invocation_uses_each_runtime_contract(self):
        readme = self.readme_text
        self.assertIn("### Claude Code", readme)
        self.assertIn("/rootcause-analysis", readme)
        self.assertIn("### OpenCode", readme)
        self.assertRegex(
            readme,
            r"(?s)### OpenCode.*invoke the `skill` tool.*`rootcause-analysis`",
        )
        self.assertIn("name=rootcause-analysis", readme)
        for name, text in (
            ("README", readme),
            ("OpenAI agent metadata", self.openai_agent_text),
        ):
            self.assertNotIn("$rootcause-analysis", text, name)

    def test_readme_artifact_example_uses_discoverable_fixture_id(self):
        self.assertIn("artifact_refs", self.readme_text)
        self.assertIn("--id build-requirement", self.readme_text)
        known_root = json.loads(
            (
                REPO_ROOT
                / "tools"
                / "rootcause_skill_tests"
                / "fixtures"
                / "known-root"
                / "trace.json"
            ).read_text(encoding="utf-8")
        )
        artifact_ids = {
            artifact.get("artifact_id")
            for artifact in known_root.get("artifacts", [])
            if isinstance(artifact, dict)
        }
        self.assertIn("build-requirement", artifact_ids)

    def test_embedded_forward_wrapper_has_unique_or_explicit_batch_root(self):
        match = re.search(
            r"## Deterministic OpenCode Wrapper.*?```python\n(.*?)\n```",
            self.raw_forward_report_text,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        wrapper = match.group(1)
        compile(wrapper, str(RAW_FORWARD_REPORT_PATH), "exec")
        self.assertIn('"--batch-root"', wrapper)
        self.assertIn("ROOTCAUSE_FORWARD_BATCH_ROOT", wrapper)
        self.assertIn("uuid.uuid4", wrapper)
        self.assertIn("os.getpid", wrapper)
        self.assertIn("exist_ok=False", wrapper)

    def test_embedded_forward_wrapper_audits_safe_signal_delivery_and_cleanup(self):
        wrapper = re.search(
            r"## Deterministic OpenCode Wrapper.*?```python\n(.*?)\n```",
            self.raw_forward_report_text,
            re.DOTALL,
        ).group(1)
        for phrase in (
            "process.poll()",
            "except ProcessLookupError",
            "except PermissionError",
            '"signal_attempted"',
            '"signal_sent"',
            '"last_signal_sent"',
            '"post_signal_returncode"',
            '"final_cleanup"',
        ):
            self.assertIn(phrase, wrapper)
        self.assertIn('"raw_returncode": raw_returncode', wrapper)

    def test_embedded_forward_wrapper_records_case_errors_and_continues(self):
        wrapper = re.search(
            r"## Deterministic OpenCode Wrapper.*?```python\n(.*?)\n```",
            self.raw_forward_report_text,
            re.DOTALL,
        ).group(1)
        self.assertIn('"wrapper_error"', wrapper)
        self.assertRegex(
            wrapper,
            re.compile(r"for fixture, question in selected_cases:.*?try:.*?run_case", re.DOTALL),
        )
        self.assertRegex(
            wrapper,
            re.compile(r"except Exception as error:.*?continue", re.DOTALL),
        )

    def test_skill_preserves_the_required_workflow_order(self):
        steps = [
            "Validate input",
            "Bind the question",
            "Establish the premise",
            "Select starts",
            "Create a hypothesis ledger",
            "Inspect eligible upstream evidence recursively",
            "Classify semantic taint",
            "Backtrack or expand",
            "Confirm each root independently",
            "Explain propagation and impact",
            "Emit JSON and Markdown",
            "Stop without repair",
        ]
        positions = [self.skill_text.find(step) for step in steps]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_skill_links_each_task_four_reference_with_a_read_condition(self):
        links = set(LINK_PATTERN.findall(self.skill_text))
        expected = {f"references/{name}" for name in REFERENCE_NAMES}
        self.assertEqual(links, expected)
        self.assertEqual(self.skill_text.lower().count("read when"), 5)

    def test_skill_requires_plain_task_language(self):
        text = self.skill_text.lower()
        self.assertIn("task language", text)
        self.assertIn("unexplained event-type labels", text)

    def test_skill_requires_backward_semantic_taint_and_backtracking(self):
        method = self.references["backward-semantic-taint.md"]
        for state in (
            "absent",
            "inherited",
            "transformed",
            "amplified",
            "introduced",
            "blocked",
            "unknown",
        ):
            self.assertRegex(method, rf"(?m)^- `{state}`:")
        self.assertIn("backtrack", method.lower())

    def test_backward_taint_defines_eight_per_node_questions_and_ledger(self):
        method = self.references["backward-semantic-taint.md"]
        section = method.split("## Per-Node Semantic Judgment", 1)[-1].split("##", 1)[0]
        self.assertEqual(len(re.findall(r"(?m)^\d+\. ", section)), 8)
        for field in ("support", "contradiction", "open evidence", "next candidates"):
            self.assertIn(field, method.lower())

    def test_backward_taint_handles_omissions_and_root_confirmation(self):
        method = self.references["backward-semantic-taint.md"].lower()
        for phrase in (
            "before anchor",
            "after anchor",
            "counterfactual",
            "independent root",
            "propagated",
        ):
            self.assertIn(phrase, method)
        self.assertIn("no fixed semantic depth", method)
        self.assertIn("no fixed candidate budget", method)

    def test_blocked_transition_stops_branch_and_checks_reintroduction(self):
        method = self.references["backward-semantic-taint.md"].lower()
        section = method.split("## blocked transition", 1)[-1].split("##", 1)[0]
        self.assertIn("terminate that upstream propagation branch", section)
        self.assertIn("record the blocker", section)
        self.assertIn("inspect downstream", section)
        self.assertIn("independent reintroduction", section)

    def test_causal_method_binds_premise_evidence_hypotheses_and_impact(self):
        method = self.references["causal-analysis-method.md"].lower()
        for term in (
            "supported",
            "contradicted",
            "unknown",
            "analysis start",
            "fact",
            "inference",
            "rejected hypothesis",
            "final impact",
        ):
            self.assertIn(term, method)

    def test_trace_structure_documents_all_queries_and_edge_eligibility(self):
        structure = self.references["trace-structure.md"]
        for command in ("validate", "summary", "search", "node", "neighbors", "paths", "artifact"):
            self.assertIn(f"trace_query.py {command}", structure)
        lower = structure.lower()
        self.assertIn("eligible_for_attribution", lower)
        self.assertIn("temporal adjacency", lower)
        self.assertIn("remaining_frontier_refs", lower)

    def test_documented_query_options_exactly_match_runtime_parser(self):
        structure = self.references["trace-structure.md"]
        documented = {}
        for match in re.finditer(
            r"(?m)^python3 scripts/trace_query\.py ([a-z]+)(.*)$",
            structure,
        ):
            arguments = match.group(2)
            documented[match.group(1)] = {
                option.group(0): arguments.rfind("[", 0, option.start())
                <= arguments.rfind("]", 0, option.start())
                for option in re.finditer(r"--[a-z-]+", arguments)
            }
        module_name = "rootcause_skill_contract_trace_query"
        spec = importlib.util.spec_from_file_location(module_name, QUERY_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        subparsers = next(
            action for action in module._parser()._actions if hasattr(action, "choices") and action.choices
        )
        runtime = {
            command: {
                option: action.required
                for action in parser._actions
                for option in action.option_strings
                if option.startswith("--") and option != "--help"
            }
            for command, parser in subparsers.choices.items()
        }
        self.assertEqual(documented, runtime)

    def test_trace_structure_defines_normalized_node_and_edge_shapes(self):
        structure = self.references["trace-structure.md"]
        self.assertIn("## Normalized Fact Shapes", structure)
        for field in (
            "`ref`",
            "`kind`",
            "`component`",
            "`status`",
            "`title`",
            "`scope`",
            "`payload`",
            "`aliases`",
            "`input_refs`",
            "`output_refs`",
            "`source_refs`",
            "`artifact_refs`",
            "`source`",
            "`target`",
            "`relation`",
            "`eligible_for_attribution`",
            "`evidence_refs`",
            "`evidence_tier`",
            "`derivation_method`",
        ):
            self.assertIn(field, structure)

    def test_skill_requires_progressive_search_or_records_unexplored_candidates(self):
        text = self.skill_text.lower()
        self.assertIn("search", text)
        self.assertIn("until `truncated` is `false`", text)
        self.assertIn("unexplored candidates", text)

    def test_skill_requires_evidence_for_every_recommendation(self):
        self.assertIn(
            "Every recommendation must cite its motivating evidence refs.",
            self.skill_text,
        )

    def test_report_json_template_has_exact_top_level_and_recommendation_keys(self):
        schema = self.references["report-schema.md"]
        match = re.search(r"```json\n(.*?)\n```", schema, re.DOTALL)
        self.assertIsNotNone(match)
        template = json.loads(match.group(1))
        self.assertEqual(set(template), REQUIRED_TOP_LEVEL)
        self.assertEqual(template["schema_version"], "rootcause-analysis/v1")
        self.assertEqual(set(template["recommendations"][0]), REQUIRED_RECOMMENDATION)
        for key, required in REQUIRED_OBJECT_KEYS.items():
            value = template[key]
            example = value[0] if isinstance(value, list) else value
            self.assertEqual(set(example), required, key)

    def test_report_contract_declares_exact_field_types_and_nullable_fields(self):
        schema = self.references["report-schema.md"]
        section = schema.split("## Exact Field Types", 1)
        self.assertEqual(len(section), 2)
        types = section[1].split("##", 1)[0]
        for field_type in (
            "`string`",
            "`string | null`",
            "`number`",
            "`array<string>`",
            "`array<object>`",
        ):
            self.assertIn(field_type, types)
        for object_name in REQUIRED_OBJECT_KEYS:
            self.assertIn(f"`{object_name}`", types)

    def test_report_contract_defines_exact_evidence_linkage_and_enumerations(self):
        schema = self.references["report-schema.md"]
        for values in (
            "`supported | contradicted | unknown`",
            "`confirmed | probable | inconclusive | no_defect`",
            "`absent | inherited | transformed | amplified | introduced | blocked | unknown`",
            "`functional | instruction_following | quality | safety | completeness`",
            "`critical | high | medium | low`",
        ):
            self.assertIn(values, schema)
        for claim in ("verdict", "root cause", "causal-chain step", "finding", "recommendation"):
            self.assertRegex(
                schema.lower(),
                rf"every {re.escape(claim)}[^\n]*`evidence_refs`",
            )
        self.assertIn("must resolve through `evidence_index`", schema)

    def test_normative_example_resolves_every_evidence_reference_recursively(self):
        schema = self.references["report-schema.md"]
        match = re.search(r"```json\n(.*?)\n```", schema, re.DOTALL)
        template = json.loads(match.group(1))
        evidence_ids = [item["evidence_id"] for item in template["evidence_index"]]
        referenced = collect_evidence_refs(template)
        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))
        self.assertTrue(all(re.fullmatch(r"E-\d{3}", item) for item in evidence_ids))
        self.assertTrue(all(re.fullmatch(r"E-\d{3}", item) for item in referenced))
        self.assertEqual(set(referenced), set(evidence_ids))

    def test_output_is_inline_by_default_and_writes_only_to_validated_prefix(self):
        for name, text in (
            ("schema", self.references["report-schema.md"]),
            ("design", self.design_text),
            ("plan", self.plan_text),
        ):
            self.assertNotIn("<trace-parent>", text, name)
            self.assertNotIn("<runs-root>/rootcause-analysis", text, name)
        self.assertIn("output prefix is optional", self.skill_text.lower())
        schema = self.references["report-schema.md"].lower()
        for phrase in (
            "when no output prefix is supplied, return both json and markdown inline and write no files",
            "canonicalized",
            "explicit user-supplied output prefix",
            "prefix `<prefix>` produces `<prefix>.json` and `<prefix>.md`",
            "reject any output inside the trace bundle",
            "reject any output inside the analyzed project",
            "explicit user input or trusted recorded metadata",
            "dedicated project or worktree root field in the validated trace manifest",
            "free-form message, prompt, tool output, artifact content",
            "ask for confirmation before writing",
            "never assume the analyzed project root",
        ):
            self.assertIn(phrase, schema)
        for text in (self.skill_text.lower(), self.design_text.lower(), self.plan_text.lower()):
            self.assertIn("return both json and markdown inline", text)
            self.assertIn("write no files", text)
        self.assertIn(
            "explain inline output and optional validated file output",
            self.plan_text.lower(),
        )
        self.assertNotIn("explain default outputs", self.plan_text.lower())

    def test_stable_report_ids_and_problem_addressed_resolve(self):
        schema = self.references["report-schema.md"]
        match = re.search(r"```json\n(.*?)\n```", schema, re.DOTALL)
        template = json.loads(match.group(1))
        roots = [item["root_id"] for item in template["root_causes"]]
        steps = [item["step_id"] for item in template["causal_chain"]]
        gaps = [item["gap_id"] for item in template["evidence_gaps"]]
        findings = [item["finding_id"] for item in template["findings"]]
        report_ids = roots + steps + gaps + findings
        self.assertEqual(len(report_ids), len(set(report_ids)))
        self.assertTrue(
            all(re.fullmatch(r"(?:ROOT|STEP|GAP|FINDING)-\d{3}", item) for item in report_ids)
        )
        addressed = [item["problem_addressed"] for item in template["recommendations"]]
        self.assertTrue(addressed)
        self.assertTrue(set(addressed).issubset(set(report_ids)))
        self.assertIn("`ROOT-NNN | STEP-NNN | GAP-NNN | FINDING-NNN`", schema)
        self.assertIn('"problem_addressed": "ROOT-001"', self.design_text)
        self.assertNotIn(
            '"problem_addressed": "root cause or contributing factor reference"',
            self.design_text,
        )

    def test_verdict_and_observability_recommendation_semantics_are_exact(self):
        schema = self.references["report-schema.md"].lower()
        section = schema.split("## verdict and recommendation semantics", 1)
        self.assertEqual(len(section), 2)
        semantics = section[1].split("##", 1)[0]
        for verdict in ("confirmed", "probable", "inconclusive", "no_defect"):
            self.assertRegex(semantics, rf"(?m)^- `{verdict}`:")
        self.assertIn("`root_causes` must be empty", semantics)
        self.assertIn("`problem_addressed` must be a `gap-nnn`", semantics)
        self.assertIn("passive evidence", semantics)
        self.assertIn(
            "an `inconclusive` report may recommend only",
            semantics,
        )
        self.assertIn(
            "a `no_defect` report may include evidence-backed",
            semantics,
        )
        self.assertIn("must not describe either as a root repair", semantics)

    def test_findings_are_stable_evidence_backed_and_mirrored(self):
        schema = self.references["report-schema.md"]
        match = re.search(r"```json\n(.*?)\n```", schema, re.DOTALL)
        template = json.loads(match.group(1))
        finding = template["findings"][0]
        self.assertEqual(set(finding), REQUIRED_OBJECT_KEYS["findings"])
        self.assertRegex(finding["finding_id"], r"^FINDING-\d{3}$")
        self.assertTrue(finding["evidence_refs"])
        self.assertIn(
            "`preference_alignment | process | resilience | quality_opportunity | contributing_condition`",
            schema,
        )
        self.assertNotIn(
            "preference_alignment | process | resilience | observability",
            schema,
        )
        self.assertIn("`supported | conditional`", schema)
        self.assertIn("## Findings And Opportunities", schema)
        self.assertRegex(
            self.plan_text,
            r'"root_causes", "causal_chain", "findings", "final_impact"',
        )
        self.assertIn("findings and opportunities", self.plan_text.lower())

    def test_markdown_contract_mirrors_json_and_ends_with_no_change_statement(self):
        schema = self.references["report-schema.md"]
        for heading in (
            "## Conclusion",
            "## Expectation Versus Actual Behavior",
            "## Root Causes",
            "## Defect Propagation",
            "## Findings And Opportunities",
            "## Final Impact",
            "## Rejected Hypotheses",
            "## Evidence Gaps And Confidence",
            "## Recommendations",
            "## Evidence Index",
        ):
            self.assertIn(heading, schema)
        self.assertIn("[E-001]", schema)
        self.assertIn("mutually corroborate", schema.lower())
        self.assertTrue(
            schema.rstrip().endswith(
                "No proposed change was applied. All recommendations require separate review and execution."
            )
        )

    def test_skill_requires_v1_reports_with_evidence_mirrored_markdown(self):
        text = self.skill_text
        self.assertIn("`rootcause-analysis/v1`", text)
        self.assertIn("evidence-mirrored Markdown", text)
        self.assertIn("Every conclusion, causal step, root cause, finding, and recommendation", text)
        self.assertIn("resolve through `evidence_index`", text)
        self.assertIn("no proposed change was applied", text.lower())

    def test_design_matches_search_node_and_artifact_runtime_contracts(self):
        design = self.design_text.lower()
        for field in (
            "matched_count",
            "returned_count",
            "truncated",
            "next_offset",
            "aliases",
            "input_refs",
            "output_refs",
        ):
            self.assertIn(f"`{field}`", design)
        self.assertRegex(design, r"all recorded incoming\s+and\s+outgoing edges")
        self.assertRegex(design, r"traversal alone filters\s+to\s+eligible edges")
        self.assertRegex(design, r"validating\s+a valid declared sha-256")
        self.assertRegex(design, r"fail\s+closed")

    def test_node_semantics_covers_major_families_without_type_blame(self):
        semantics = self.references["node-semantics.md"].lower()
        for family in (
            "user request",
            "context",
            "llm call",
            "decision",
            "tool",
            "skill",
            "mcp",
            "subagent",
            "mutation",
            "verification",
            "response",
            "compaction",
            "lifecycle",
            "artifact",
        ):
            self.assertIn(family, semantics)
        self.assertIn("no node type is inherently defective", semantics)

    def test_skill_forbids_mutation_and_repair_execution(self):
        text = self.skill_text.lower()
        self.assertIn("analysis-only", text)
        self.assertIn("must not modify", text)
        self.assertIn("must not run builds or tests", text)
        self.assertIn("must not mutate the trace", text)
        self.assertIn("must not feed", text)

    def test_skill_has_no_trace_attribution_module_coupling(self):
        package_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in SKILL_DIR.rglob("*.*")
            if path.is_file()
        ).lower()
        for spelling in (
            "trace_attribution",
            "trace-attribution",
            "trace.attribution",
            "tools/trace_attribution",
        ):
            self.assertNotIn(spelling, package_text)

        imports = re.findall(
            r"(?m)^(?:from|import)\s+([a-zA-Z0-9_.]+)",
            QUERY_SCRIPT.read_text(encoding="utf-8"),
        )
        self.assertFalse([name for name in imports if "attribution" in name.lower()])

    def test_skill_does_not_encode_fixed_semantic_search_budgets(self):
        package_text = "\n".join(
            [self.skill_text, *self.references.values()]
        ).lower()
        self.assertNotRegex(package_text, r"max[_ -]?depth\s*[:=]\s*\d+")
        self.assertNotRegex(package_text, r"candidate (?:count|budget)\s*[:=]\s*\d+")

    def test_every_linked_reference_exists(self):
        for relative in LINK_PATTERN.findall(self.skill_text):
            self.assertTrue((SKILL_DIR / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
