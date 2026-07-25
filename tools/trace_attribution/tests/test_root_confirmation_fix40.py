from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePath

from tools.trace_attribution.scripts.evaluate_recursive_attribution import (
    EvaluationSchemaError,
    REPORT_KEYS,
    _validate_report_shape,
)
from trace_attribution import causal_state
from trace_attribution.causal_state import (
    LEGACY_REPORT_SCHEMA_VERSION,
    MODERN_REPORT_SCHEMA_VERSION,
    PREVIOUS_REPORT_SCHEMA_VERSION,
    RecursiveAttributionReport,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from tools.trace_attribution.tests.test_root_confirmation_fix21 import (
    InjectedCheckpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix39 import (
    _run_shared_root,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
)


EXPECTED_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "objective",
        "start_refs",
        "seed_results",
        "analysis_outcome",
        "analysis_perspective",
        "defect_states",
        "causal_candidates",
        "causal_relations",
        "step_judgments",
        "hypotheses",
        "introduction_candidates",
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "rejected_candidates",
        "unresolved_hypotheses",
        "root_causes",
        "taint_paths",
        "visited_order",
        "visited_entries",
        "unresolved_refs",
        "investigation_journal",
        "metadata",
    }
)
OBJECT_COLLECTION_FIELDS = frozenset(
    {
        "seed_results",
        "defect_states",
        "causal_candidates",
        "causal_relations",
        "step_judgments",
        "hypotheses",
        "introduction_candidates",
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "rejected_candidates",
        "unresolved_hypotheses",
        "root_causes",
        "visited_entries",
        "investigation_journal",
    }
)
STRING_COLLECTION_FIELDS = frozenset(
    {"start_refs", "visited_order", "unresolved_refs"}
)
PATH_COLLECTION_FIELDS = frozenset({"taint_paths"})
COLLECTION_FIELDS = frozenset(
    {
        *OBJECT_COLLECTION_FIELDS,
        *STRING_COLLECTION_FIELDS,
        *PATH_COLLECTION_FIELDS,
    }
)
STRING_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "objective",
        "analysis_outcome",
        "analysis_perspective",
    }
)


def _modern_report_keys():
    return getattr(causal_state, "MODERN_REPORT_KEYS", frozenset())


def _validate_modern_shape(payload):
    validator = getattr(causal_state, "validate_modern_report_shape", None)
    if validator is None:
        raise AssertionError("canonical modern report shape validator is missing")
    return validator(payload)


class StrictModernReportShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = tempfile.TemporaryDirectory()
        cls.trace = shared_root_trace()
        cls.graph = TraceGraph.from_trace(cls.trace)
        cls.start_refs = ("record:seed_one", "record:seed_two")
        cls.objective = "Confirm each identical claim independently."
        cls.config = shared_root_checkpoint_config(
            cls.trace,
            cls.objective,
            cls.start_refs,
        )
        cls.root = Path(cls.resources.name) / "fix40.checkpoint"
        _, _, report = _run_shared_root(
            checkpoint=CheckpointBundle(cls.root),
            checkpoint_config=cls.config,
        )
        cls.payload = report.to_dict()
        cls.checkpoint = CheckpointBundle(cls.root).restore(
            expected_config=cls.config
        )

    @classmethod
    def tearDownClass(cls):
        cls.resources.cleanup()

    def restore_report(self, payload, *, completed):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        action = next(
            item
            for item in reversed(actions)
            if item["semantic_key"] == "analysis:result"
        )
        action["payload"]["report"] = copy.deepcopy(payload)
        action["payload"]["interrupted"] = False
        action["operation"] = (
            "analysis_completed" if completed else "analysis_ready"
        )
        checkpoint = replace(self.checkpoint, actions=tuple(actions))
        return AgenticRecursiveAnalyzer(
            judge=SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
            checkpoint=InjectedCheckpoint(checkpoint, self.root),
            checkpoint_config=self.config,
        ).analyze(
            self.graph,
            start_refs=self.start_refs,
            objective=self.objective,
            analysis_perspective="",
        )

    def assert_rejected_everywhere(self, payload, *, checkpoint=True):
        with self.assertRaises((TypeError, ValueError)):
            RecursiveAttributionReport.from_dict(copy.deepcopy(payload))
        with self.assertRaises(EvaluationSchemaError):
            _validate_report_shape(
                copy.deepcopy(payload),
                {"case_id": self.payload["case_id"]},
            )
        if checkpoint:
            for completed in (False, True):
                with self.subTest(completed=completed):
                    with self.assertRaises((TypeError, ValueError)):
                        self.restore_report(payload, completed=completed)

    def test_one_canonical_key_set_is_shared_by_writer_reader_and_evaluator(self):
        self.assertEqual(_modern_report_keys(), EXPECTED_REPORT_KEYS)
        self.assertIs(REPORT_KEYS, _modern_report_keys())
        self.assertEqual(set(self.payload), EXPECTED_REPORT_KEYS)
        _validate_modern_shape(self.payload)
        RecursiveAttributionReport.from_dict(copy.deepcopy(self.payload))
        _validate_report_shape(
            copy.deepcopy(self.payload),
            {"case_id": self.payload["case_id"]},
        )

    def test_non_string_top_level_key_cannot_shadow_a_canonical_field(self):
        payload = copy.deepcopy(self.payload)
        payload[PurePath("case_id")] = "shadow-case-id"

        self.assert_rejected_everywhere(payload)

    def test_mapping_journal_entries_round_trip_without_silent_loss(self):
        payload = copy.deepcopy(self.payload)
        payload["investigation_journal"] = [
            causal_state.FrozenMapping(item)
            for item in payload["investigation_journal"]
        ]

        _validate_modern_shape(payload)
        _validate_report_shape(
            payload,
            {"case_id": self.payload["case_id"]},
        )
        restored = RecursiveAttributionReport.from_dict(payload)

        self.assertEqual(
            restored.to_dict()["investigation_journal"],
            self.payload["investigation_journal"],
        )

    def test_every_missing_or_unknown_top_level_field_is_rejected(self):
        for field in sorted(EXPECTED_REPORT_KEYS):
            with self.subTest(missing=field):
                payload = copy.deepcopy(self.payload)
                payload.pop(field)
                self.assert_rejected_everywhere(payload)

        payload = copy.deepcopy(self.payload)
        payload["unknown_top_level"] = []
        self.assert_rejected_everywhere(payload)

    def test_every_collection_field_requires_an_array(self):
        for field in sorted(COLLECTION_FIELDS):
            for invalid in (None, {}, "not-an-array"):
                with self.subTest(field=field, invalid=type(invalid).__name__):
                    payload = copy.deepcopy(self.payload)
                    payload[field] = invalid
                    self.assert_rejected_everywhere(payload)

    def test_every_object_collection_rejects_non_object_elements(self):
        for field in sorted(OBJECT_COLLECTION_FIELDS):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload[field].append("not-an-object")
                self.assert_rejected_everywhere(payload)

    def test_string_path_metadata_and_scalar_types_are_exact(self):
        for field in sorted(STRING_COLLECTION_FIELDS):
            with self.subTest(string_collection=field):
                payload = copy.deepcopy(self.payload)
                payload[field].append(7)
                self.assert_rejected_everywhere(payload)

        for invalid_path in ("not-a-path-array", ["record:valid", 7]):
            with self.subTest(path=invalid_path):
                payload = copy.deepcopy(self.payload)
                payload["taint_paths"].append(invalid_path)
                self.assert_rejected_everywhere(payload)

        for field in sorted(STRING_FIELDS):
            with self.subTest(string_field=field):
                payload = copy.deepcopy(self.payload)
                payload[field] = 7
                self.assert_rejected_everywhere(payload)

        payload = copy.deepcopy(self.payload)
        payload["metadata"] = []
        self.assert_rejected_everywhere(payload)

    def test_checkpoint_report_envelope_rejects_non_object_payloads(self):
        for invalid in (None, [], "not-a-report"):
            for completed in (False, True):
                with self.subTest(
                    invalid=type(invalid).__name__,
                    completed=completed,
                ):
                    with self.assertRaises((TypeError, ValueError)):
                        self.restore_report(invalid, completed=completed)

    def test_versioned_legacy_and_v2_migrations_remain_explicit(self):
        legacy = {
            "schema_version": LEGACY_REPORT_SCHEMA_VERSION,
            "case_id": "legacy-case",
            "objective": "Preserve legacy evidence gaps.",
            "root_causes": [],
            "metadata": {},
        }
        migrated_legacy = RecursiveAttributionReport.from_dict(legacy)
        self.assertEqual(
            migrated_legacy.schema_version,
            MODERN_REPORT_SCHEMA_VERSION,
        )

        previous = copy.deepcopy(self.payload)
        previous["schema_version"] = PREVIOUS_REPORT_SCHEMA_VERSION
        migrated_previous = RecursiveAttributionReport.from_dict(previous)
        self.assertEqual(
            migrated_previous.schema_version,
            MODERN_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(
            migrated_previous.metadata["report_migration"]["source_schema"],
            PREVIOUS_REPORT_SCHEMA_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
