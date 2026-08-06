from __future__ import annotations

import copy
import unittest

from trace_attribution.restoration_obligation import (
    OFFLINE_JUDGE_VISIBILITY,
    RESTORATION_OBLIGATION_SCHEMA,
    RestorationObligation,
)


class RestorationObligationTest(unittest.TestCase):
    def _create(self, **overrides):
        values = {
            "obligation_id": "restore-model-construction-contract",
            "kind": "observed_defect_remediation",
            "baseline_state": "dependency_closure_missing",
            "required_end_state": "model construction dependency closure restored",
            "required_capabilities": (
                "namespace_resolution",
                "model_rebuild",
            ),
            "scope_refs": (
                "record:seed",
                "record:initial_prompt",
            ),
            "acceptance_evidence_refs": (
                "record:failed_evaluation",
                "artifact:test_output",
            ),
            "provenance": {
                "source": "external_quality_review",
                "source_refs": (
                    "record:failed_evaluation",
                    "record:seed",
                ),
                "derivation": "reviewed benchmark failure contract",
            },
        }
        values.update(overrides)
        return RestorationObligation.create(**values)

    def test_create_normalizes_set_like_facts_and_has_stable_identity(self):
        first = self._create(
            obligation_id=" restore-model-construction-contract ",
            required_capabilities=(
                "namespace_resolution",
                " model_rebuild ",
                "namespace_resolution",
            ),
            scope_refs=(
                " record:seed ",
                "record:initial_prompt",
                "record:seed",
            ),
            acceptance_evidence_refs=(
                "record:failed_evaluation",
                " artifact:test_output ",
                "record:failed_evaluation",
            ),
            provenance={
                "source": " external_quality_review ",
                "source_refs": (
                    "record:seed",
                    " record:failed_evaluation ",
                    "record:seed",
                ),
                "derivation": " reviewed benchmark failure contract ",
            },
        )
        reordered = self._create(
            required_capabilities=(
                "model_rebuild",
                "namespace_resolution",
            ),
            scope_refs=(
                "record:initial_prompt",
                "record:seed",
            ),
            acceptance_evidence_refs=(
                "artifact:test_output",
                "record:failed_evaluation",
            ),
            provenance={
                "source": "external_quality_review",
                "source_refs": (
                    "record:seed",
                    "record:failed_evaluation",
                ),
                "derivation": "reviewed benchmark failure contract",
            },
        )

        self.assertEqual(first, reordered)
        self.assertEqual(first.identity, reordered.identity)
        self.assertEqual(len(first.identity), 64)
        self.assertEqual(
            first.required_capabilities,
            ("model_rebuild", "namespace_resolution"),
        )
        self.assertEqual(
            first.scope_refs,
            ("record:initial_prompt", "record:seed"),
        )
        self.assertEqual(first.visibility, OFFLINE_JUDGE_VISIBILITY)
        self.assertNotEqual(
            first.identity,
            self._create(
                required_end_state="a different required end state"
            ).identity,
        )

    def test_round_trip_uses_an_exact_json_schema(self):
        obligation = self._create()
        payload = obligation.to_dict()

        self.assertEqual(
            set(payload),
            {
                "schema",
                "obligation_id",
                "kind",
                "baseline_state",
                "required_end_state",
                "required_capabilities",
                "scope_refs",
                "acceptance_evidence_refs",
                "provenance",
                "visibility",
                "identity",
            },
        )
        self.assertEqual(payload["schema"], RESTORATION_OBLIGATION_SCHEMA)
        self.assertIsInstance(payload["required_capabilities"], list)
        self.assertIsInstance(payload["scope_refs"], list)
        self.assertIsInstance(payload["acceptance_evidence_refs"], list)
        self.assertIsInstance(payload["provenance"]["source_refs"], list)
        self.assertEqual(
            RestorationObligation.from_dict(copy.deepcopy(payload)),
            obligation,
        )

    def test_unknown_or_missing_fields_are_rejected_at_every_schema_level(self):
        payload = self._create().to_dict()
        mutations = []

        extra_top_level = copy.deepcopy(payload)
        extra_top_level["unexpected"] = True
        mutations.append(extra_top_level)

        missing_top_level = copy.deepcopy(payload)
        missing_top_level.pop("baseline_state")
        mutations.append(missing_top_level)

        extra_provenance = copy.deepcopy(payload)
        extra_provenance["provenance"]["unexpected"] = True
        mutations.append(extra_provenance)

        missing_provenance = copy.deepcopy(payload)
        missing_provenance["provenance"].pop("derivation")
        mutations.append(missing_provenance)

        for mutated in mutations:
            with self.subTest(keys=sorted(mutated)):
                with self.assertRaisesRegex(ValueError, "schema mismatch"):
                    RestorationObligation.from_dict(mutated)

    def test_schema_visibility_identity_and_json_array_contract_are_strict(self):
        payload = self._create().to_dict()

        wrong_schema = copy.deepcopy(payload)
        wrong_schema["schema"] = "restoration-obligation/v0"
        with self.assertRaisesRegex(ValueError, "schema is unsupported"):
            RestorationObligation.from_dict(wrong_schema)

        wrong_visibility = copy.deepcopy(payload)
        wrong_visibility["visibility"] = "agent_visible"
        with self.assertRaisesRegex(ValueError, "offline_judge_only"):
            RestorationObligation.from_dict(wrong_visibility)

        wrong_identity = copy.deepcopy(payload)
        wrong_identity["identity"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            RestorationObligation.from_dict(wrong_identity)

        non_json_array = copy.deepcopy(payload)
        non_json_array["scope_refs"] = tuple(non_json_array["scope_refs"])
        with self.assertRaisesRegex(TypeError, "JSON array"):
            RestorationObligation.from_dict(non_json_array)

    def test_required_fact_collections_and_strings_cannot_be_empty(self):
        cases = {
            "obligation_id": {"obligation_id": " "},
            "required_capabilities": {"required_capabilities": ()},
            "scope_refs": {"scope_refs": ()},
            "acceptance_evidence_refs": {
                "acceptance_evidence_refs": ()
            },
            "provenance source_refs": {
                "provenance": {
                    "source": "external_quality_review",
                    "source_refs": (),
                    "derivation": "reviewed benchmark failure contract",
                }
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    self._create(**overrides)

    def test_resolver_canonicalizes_create_and_validates_deserialization(self):
        aliases = {
            "prompt-alias": "record:initial_prompt",
            "seed-alias": "record:seed",
            "evaluation-alias": "record:failed_evaluation",
            "output-alias": "artifact:test_output",
            "record:initial_prompt": "record:initial_prompt",
            "record:seed": "record:seed",
            "record:failed_evaluation": "record:failed_evaluation",
            "artifact:test_output": "artifact:test_output",
        }

        def resolver(ref):
            return aliases.get(ref)

        obligation = self._create(
            scope_refs=("seed-alias", "prompt-alias"),
            acceptance_evidence_refs=(
                "evaluation-alias",
                "output-alias",
            ),
            provenance={
                "source": "external_quality_review",
                "source_refs": ("seed-alias", "evaluation-alias"),
                "derivation": "reviewed benchmark failure contract",
            },
            resolver=resolver,
        )

        self.assertEqual(
            obligation.scope_refs,
            ("record:initial_prompt", "record:seed"),
        )
        self.assertEqual(
            RestorationObligation.from_dict(
                obligation.to_dict(), resolver=resolver
            ),
            obligation,
        )

        with self.assertRaisesRegex(ValueError, "could not be resolved"):
            self._create(
                scope_refs=("record:missing",),
                resolver=resolver,
            )

        noncanonical = obligation.to_dict()
        noncanonical["scope_refs"][0] = "prompt-alias"
        with self.assertRaisesRegex(ValueError, "must contain canonical refs"):
            RestorationObligation.from_dict(
                noncanonical, resolver=resolver
            )


if __name__ == "__main__":
    unittest.main()
