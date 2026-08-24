import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "rootcause-analysis"
SKILL_PATH = SKILL_DIR / "SKILL.md"
REFERENCE_NAMES = {
    "backward-semantic-taint.md",
    "causal-analysis-method.md",
    "node-semantics.md",
    "trace-structure.md",
}
LINK_PATTERN = re.compile(r"\[[^\]]+\]\((references/[^)#]+\.md)(?:#[^)]+)?\)")


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


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_text = SKILL_PATH.read_text(encoding="utf-8")
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
        self.assertEqual(self.skill_text.lower().count("read when"), 4)
        self.assertNotIn("report-schema.md", self.skill_text)

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

    def test_skill_does_not_import_or_invoke_trace_attribution(self):
        package_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in SKILL_DIR.rglob("*.*")
            if path.is_file()
        )
        self.assertNotIn("python3 -m trace_attribution", package_text)
        self.assertNotIn("from trace_attribution", package_text)

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
