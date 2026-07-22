import json
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_state import (
    AttributionHypothesis,
    DefectState,
    FrontierItem,
    HypothesisEvidence,
)
from trace_attribution.hypotheses import HypothesisLedger, RecursiveFrontier, hypothesis_order_key


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "checkpoints"


def sample_defect_state(label="missing_namespace_contract"):
    return DefectState.create(
        label=label,
        expected="The parser preserves the namespace compatibility contract.",
        actual="The namespace compatibility method is absent.",
        mechanism="implementation planning omitted a called method",
        scope="parser_contract_recovery",
    )


def item_for(node_ref, defect_state, hypothesis, *, priority=0.75, depth=1, graph_position=4):
    return FrontierItem.create(
        node_ref=node_ref,
        defect_state=defect_state,
        downstream_path=["record:observed", node_ref],
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_semantic_hash=hypothesis.semantic_hash,
        seed_binding_identity=hypothesis.seed_binding_identity,
        depth=depth,
        candidate_source="confirmed_edge",
        priority=priority,
        graph_position=graph_position,
    )


class HypothesisLedgerTest(unittest.TestCase):
    def test_rejected_leading_hypothesis_restores_best_active_alternative(self):
        ledger = HypothesisLedger()
        first = ledger.create("prompt omission is root", "record:prompt", sample_defect_state())
        second = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        first = ledger.add_support(first.hypothesis_id, "record:prompt", "prompt omits method", 0.8)
        second = ledger.add_support(
            second.hypothesis_id, "record:decision", "plan declares completion", 0.75
        )

        ledger.reject(first.hypothesis_id, "repository search could avoid the failure")

        self.assertEqual(ledger.best_active().hypothesis_id, second.hypothesis_id)
        self.assertEqual(ledger.get(first.hypothesis_id).status, "rejected")

    def test_evidence_is_deduplicated_by_normalized_semantics_with_strongest_confidence(self):
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )

        updated = ledger.add_support(
            hypothesis.hypothesis_id, "record:decision", "Plan declares completion", 0.4
        )
        updated = ledger.add_support(
            updated.hypothesis_id, " record:decision ", " plan   declares completion ", 0.9
        )
        updated = ledger.add_opposition(
            updated.hypothesis_id, "record:prompt", "Prompt permits a complete search", 0.2
        )
        updated = ledger.add_opposition(
            updated.hypothesis_id, " record:prompt ", " prompt permits  a complete search ", 0.95
        )

        self.assertEqual(len(updated.supporting_evidence), 1)
        self.assertEqual(len(updated.opposing_evidence), 1)
        self.assertEqual(updated.supporting_evidence[0].ref, "record:decision")
        self.assertEqual(updated.supporting_evidence[0].reason, "Plan declares completion")
        self.assertEqual(updated.opposing_evidence[0].ref, "record:prompt")
        self.assertEqual(updated.opposing_evidence[0].reason, "Prompt permits a complete search")
        self.assertIn(updated.supporting_evidence[0].ref, {"record:decision", "record:prompt"})
        self.assertEqual(updated.supporting_evidence[0].confidence, 0.9)
        self.assertEqual(updated.opposing_evidence[0].confidence, 0.95)
        self.assertEqual(hypothesis_order_key(updated)[0], -0.9)
        self.assertEqual(updated.status, "supported")
        self.assertNotEqual(updated.status, "confirmed")

    def test_unresolved_question_rekeys_semantic_identity_and_restores_snapshot(self):
        ledger = HypothesisLedger()
        frontier = RecursiveFrontier()
        first = ledger.create("prompt omission is root", "record:prompt", sample_defect_state())
        second = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        first = ledger.supersede(
            first.hypothesis_id, second.hypothesis_id, "search closure explains more evidence"
        )

        updated = ledger.add_unresolved_question(
            second.hypothesis_id, "Were repository call sites searched?", frontier=frontier
        )
        restored = HypothesisLedger.from_snapshot(ledger.snapshot())

        self.assertNotEqual(updated.hypothesis_id, second.hypothesis_id)
        self.assertEqual(
            restored.get(first.hypothesis_id).alternative_hypothesis_ids,
            (updated.hypothesis_id,),
        )
        self.assertEqual(restored.snapshot(), ledger.snapshot())

    def test_unresolved_question_requires_a_frontier_for_semantic_rekeying(self):
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )

        with self.assertRaises(TypeError):
            ledger.add_unresolved_question(hypothesis.hypothesis_id, "Were call sites searched?")

    def test_snapshot_rejects_noncanonical_or_duplicate_evidence(self):
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        snapshot = ledger.snapshot()

        for field in ("supporting_evidence", "opposing_evidence"):
            payload = dict(snapshot[0])
            payload[field] = [
                {
                    "ref": " record:decision ",
                    "reason": " Plan   declares completion ",
                    "confidence": 0.4,
                },
                {
                    "ref": "record:decision",
                    "reason": "plan declares completion",
                    "confidence": 0.9,
                },
            ]
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "canonical evidence"):
                    HypothesisLedger.from_snapshot([payload])

    def test_snapshot_rejects_whitespace_polluted_evidence_ref(self):
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        payload = dict(ledger.snapshot()[0])
        payload["supporting_evidence"] = [
            {
                "ref": " record:decision ",
                "reason": "Plan declares completion",
                "confidence": 0.9,
            }
        ]

        with self.assertRaisesRegex(ValueError, "canonical evidence"):
            HypothesisLedger.from_snapshot([payload])

    def test_explicit_frontier_transition_migrates_queued_item_identity(self):
        ledger = HypothesisLedger()
        frontier = RecursiveFrontier()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        queued = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(queued)

        updated = ledger.add_unresolved_question(
            hypothesis.hypothesis_id,
            "Were repository call sites searched?",
            frontier=frontier,
        )
        popped = frontier.pop()

        self.assertNotEqual(popped.hypothesis_id, hypothesis.hypothesis_id)
        self.assertEqual(ledger.get(popped.hypothesis_id), updated)
        self.assertEqual(popped.hypothesis_semantic_hash, updated.semantic_hash)
        self.assertEqual(popped.visit_key, FrontierItem.create(
            node_ref=popped.node_ref,
            defect_state=popped.defect_state,
            downstream_path=list(popped.downstream_path),
            hypothesis_id=updated.hypothesis_id,
            hypothesis_semantic_hash=updated.semantic_hash,
            depth=popped.depth,
            candidate_source=popped.candidate_source,
            priority=popped.priority,
            checked_evidence_refs=list(popped.checked_evidence_refs),
            evidence_hash=popped.evidence_hash,
            reopen_reason=popped.reopen_reason,
            graph_position=popped.graph_position,
        ).visit_key)

    def test_frontier_transition_checkpoint_restore_keeps_completed_visit_migrated(self):
        ledger = HypothesisLedger()
        frontier = RecursiveFrontier()
        hypothesis = ledger.create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:checkpoint-transition",
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        frontier.mark_completed(frontier.pop(), "evidence:v1")

        updated = ledger.add_unresolved_question(
            hypothesis.hypothesis_id,
            "Were repository call sites searched?",
            frontier=frontier,
        )
        migrated = item_for("record:decision", sample_defect_state(), updated)
        restored_ledger = HypothesisLedger.from_snapshot(ledger.snapshot())
        restored_frontier = RecursiveFrontier.from_checkpoint(
            frontier.checkpoint(),
            hypotheses_by_id=restored_ledger.hypotheses_by_id(),
        )

        self.assertEqual(restored_ledger.get(migrated.hypothesis_id).semantic_hash, migrated.hypothesis_semantic_hash)
        self.assertFalse(restored_frontier.push(migrated))
        self.assertTrue(
            restored_frontier.reopen(migrated, evidence_hash="evidence:v2", reason="new evidence")
        )

    def test_best_active_orders_support_then_open_questions_then_stable_identity(self):
        ledger = HypothesisLedger()
        weaker = ledger.create("A root", "record:a", sample_defect_state())
        stronger = ledger.create("B root", "record:b", sample_defect_state())
        weaker = ledger.add_support(weaker.hypothesis_id, "record:a", "partial support", 0.4)
        stronger = ledger.add_support(stronger.hypothesis_id, "record:b", "strong support", 0.8)
        stronger = ledger.add_unresolved_question(
            stronger.hypothesis_id, "Check an artifact", frontier=RecursiveFrontier()
        )

        self.assertLess(hypothesis_order_key(stronger), hypothesis_order_key(weaker))
        self.assertEqual(ledger.best_active().hypothesis_id, stronger.hypothesis_id)


class RecursiveFrontierTest(unittest.TestCase):
    def test_frontier_merges_same_visit_but_keeps_defect_transformations(self):
        frontier = RecursiveFrontier()
        hypothesis = HypothesisLedger().create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        defect_a = sample_defect_state()
        defect_b = defect_a.transformed(
            label="premature_search_closure",
            mechanism="the plan stopped before searching callers",
            transformation_reason="the incomplete search produced the incomplete plan",
        )

        self.assertTrue(frontier.push(item_for("record:decision", defect_a, hypothesis)))
        self.assertFalse(frontier.push(item_for("record:decision", defect_a, hypothesis)))
        self.assertTrue(frontier.push(item_for("record:decision", defect_b, hypothesis)))

    def test_frontier_pops_priority_then_depth_then_graph_position(self):
        frontier = RecursiveFrontier()
        ledger = HypothesisLedger()
        hypothesis = ledger.create("agent search closure is root", "record:decision", sample_defect_state())
        high = item_for("record:high", sample_defect_state("high"), hypothesis, priority=0.9)
        shallow = item_for("record:shallow", sample_defect_state("shallow"), hypothesis, depth=1)
        deep = item_for("record:deep", sample_defect_state("deep"), hypothesis, depth=2)

        frontier.push(deep)
        frontier.push(shallow)
        frontier.push(high)

        self.assertEqual([frontier.pop().node_ref for _ in range(3)], ["record:high", "record:shallow", "record:deep"])

    def test_reopen_requires_changed_evidence_or_root_verifier_rejection(self):
        frontier = RecursiveFrontier()
        hypothesis = HypothesisLedger().create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        frontier.mark_completed(frontier.pop(), "evidence:v1")

        self.assertFalse(frontier.reopen(item, evidence_hash="evidence:v1", reason="unchanged"))
        self.assertTrue(frontier.reopen(item, evidence_hash="evidence:v2", reason="artifact expanded"))
        reopened = frontier.pop()
        frontier.mark_completed(reopened, "evidence:v2")
        self.assertTrue(
            frontier.reopen(
                reopened,
                evidence_hash="evidence:v2",
                reason="root verifier rejected the candidate",
                root_verifier_rejected=True,
            )
        )

    def test_mark_completed_requires_the_exact_in_flight_item(self):
        frontier = RecursiveFrontier()
        hypothesis = HypothesisLedger().create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        popped = frontier.pop()

        with self.assertRaisesRegex(ValueError, "in-flight"):
            frontier.mark_completed(replace(popped, priority=0.1), "evidence:v1")

        frontier.mark_completed(popped, "evidence:v1")
        self.assertFalse(frontier.push(popped))

    def test_frontier_transition_migrates_in_flight_item_before_completion(self):
        ledger = HypothesisLedger()
        frontier = RecursiveFrontier()
        hypothesis = ledger.create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        popped = frontier.pop()

        updated = ledger.add_unresolved_question(
            hypothesis.hypothesis_id,
            "Were repository call sites searched?",
            frontier=frontier,
        )
        migrated = frontier.in_flight_items()[0]

        self.assertEqual(migrated.hypothesis_id, updated.hypothesis_id)
        with self.assertRaisesRegex(ValueError, "in-flight"):
            frontier.mark_completed(popped, "evidence:v1")
        frontier.mark_completed(migrated, "evidence:v1")

    def test_checkpoint_requeues_interrupted_in_flight_work_deterministically(self):
        frontier = RecursiveFrontier()
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:interrupted",
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        frontier.pop()

        restored = RecursiveFrontier.from_checkpoint(
            frontier.checkpoint(), hypotheses_by_id=ledger.hypotheses_by_id()
        )

        self.assertFalse(restored.in_flight_items())
        self.assertEqual(restored.pop(), item)

    def test_checkpoint_requires_current_schema_and_all_lifecycle_sections(self):
        frontier = RecursiveFrontier()
        hypothesis = HypothesisLedger().create(
            "agent search closure is root", "record:decision", sample_defect_state()
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        completed = frontier.pop()
        frontier.mark_completed(completed, "evidence:v1")
        checkpoint = frontier.checkpoint()

        for section in ("queued", "in_flight", "completed"):
            malformed = dict(checkpoint)
            malformed.pop(section)
            with self.subTest(section=section):
                with self.assertRaisesRegex(ValueError, "missing"):
                    RecursiveFrontier.from_checkpoint(
                        malformed, hypotheses_by_id={}
                    )

        malformed = dict(checkpoint)
        malformed["completed"] = {}
        with self.assertRaisesRegex(ValueError, "list"):
            RecursiveFrontier.from_checkpoint(malformed, hypotheses_by_id={})

        unknown_version = dict(checkpoint)
        unknown_version["version"] = checkpoint["version"] + 1
        with self.assertRaisesRegex(ValueError, "version"):
            RecursiveFrontier.from_checkpoint(
                unknown_version, hypotheses_by_id={}
            )

        self.assertFalse(frontier.push(completed))

    def test_checkpoint_version_must_be_the_exact_supported_integer(self):
        checkpoint = RecursiveFrontier().checkpoint()
        invalid_versions = (True, 1.0, "1", None, 3)

        for version in invalid_versions:
            payload = dict(checkpoint)
            if version is None:
                payload.pop("version")
            else:
                payload["version"] = version
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "version"):
                    RecursiveFrontier.from_checkpoint(
                        payload, hypotheses_by_id={}
                    )

    def test_v1_checkpoint_fixture_validates_legacy_identity_then_migrates_seed_binding(self):
        fixture = json.loads(
            (FIXTURE_ROOT / "frontier-v1-pre-seed-binding.json").read_text(
                encoding="utf-8"
            )
        )
        payload = fixture["frontier"]
        hypothesis = AttributionHypothesis.from_dict(fixture["hypothesis"])

        frontier = RecursiveFrontier.from_checkpoint(
            payload,
            hypotheses_by_id={hypothesis.hypothesis_id: hypothesis},
        )

        item = frontier.pop()
        self.assertEqual(frontier.checkpoint()["version"], 2)
        self.assertEqual(item.seed_binding_identity, "seed:legacy-seed-one")
        self.assertNotEqual(
            item.visit_key,
            payload["queued"][0]["visit_key"],
        )

    def test_v1_checkpoint_rejects_forged_legacy_identity_before_migration(self):
        fixture = json.loads(
            (FIXTURE_ROOT / "frontier-v1-pre-seed-binding.json").read_text(
                encoding="utf-8"
            )
        )
        payload = fixture["frontier"]
        hypothesis = AttributionHypothesis.from_dict(fixture["hypothesis"])
        payload["queued"][0]["item_id"] = "frontier:forged"

        with self.assertRaisesRegex(ValueError, "legacy FrontierItem item_id"):
            RecursiveFrontier.from_checkpoint(
                payload,
                hypotheses_by_id={hypothesis.hypothesis_id: hypothesis},
            )

    def test_checkpoint_rejects_empty_or_mismatched_frontier_binding(self):
        hypothesis = HypothesisLedger().create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:one",
        )
        hypotheses = {hypothesis.hypothesis_id: hypothesis}

        for version, binding in ((2, ""), (2, "seed:other"), (1, "seed:other")):
            item = FrontierItem.create(
                node_ref="record:decision",
                defect_state=sample_defect_state(),
                downstream_path=["record:observed", "record:decision"],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=binding,
            )
            payload = RecursiveFrontier().checkpoint()
            payload["version"] = version
            payload["queued"] = [item.to_dict()]

            with self.subTest(version=version, binding=binding):
                with self.assertRaisesRegex(ValueError, "seed binding"):
                    RecursiveFrontier.from_checkpoint(
                        payload, hypotheses_by_id=hypotheses
                    )

    def test_checkpoint_rejects_reused_hypothesis_id_with_different_semantic_hash(self):
        ledger = HypothesisLedger()
        enclosing = ledger.create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:one",
        )
        different = AttributionHypothesis.create(
            "prompt omission is root",
            "record:prompt",
            sample_defect_state(),
            seed_binding_identity="seed:one",
        )
        forged = FrontierItem.create(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            downstream_path=["record:observed", "record:decision"],
            hypothesis_id=enclosing.hypothesis_id,
            hypothesis_semantic_hash=different.semantic_hash,
            seed_binding_identity=enclosing.seed_binding_identity,
        )
        for version in (1, 2):
            payload = RecursiveFrontier().checkpoint()
            payload["version"] = version
            payload["queued"] = [forged.to_dict()]

            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "semantic hash"):
                    RecursiveFrontier.from_checkpoint(
                        payload,
                        hypotheses_by_id={enclosing.hypothesis_id: enclosing},
                    )

    def test_v1_checkpoint_rejects_duplicate_legacy_visit_key_before_migration(self):
        fixture = json.loads(
            (FIXTURE_ROOT / "frontier-v1-pre-seed-binding.json").read_text(
                encoding="utf-8"
            )
        )
        first = AttributionHypothesis.from_dict(fixture["hypothesis"])
        second = AttributionHypothesis.create(
            first.claim,
            first.candidate_root_ref,
            DefectState.from_dict(fixture["frontier"]["queued"][0]["defect_state"]),
            seed_binding_identity="seed:legacy-seed-two",
        )
        duplicate = dict(fixture["frontier"]["queued"][0])
        duplicate["hypothesis_id"] = second.hypothesis_id
        fixture["frontier"]["in_flight"] = [duplicate]

        with self.assertRaisesRegex(ValueError, "duplicate legacy visit_key"):
            RecursiveFrontier.from_checkpoint(
                fixture["frontier"],
                hypotheses_by_id={
                    first.hypothesis_id: first,
                    second.hypothesis_id: second,
                },
            )

    def test_corrupt_checkpoint_and_reopen_rollback_preserve_completed_state(self):
        frontier = RecursiveFrontier()
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:corrupt",
        )
        item = item_for("record:decision", sample_defect_state(), hypothesis)
        frontier.push(item)
        completed = frontier.pop()
        frontier.mark_completed(completed, "evidence:v1")
        checkpoint = frontier.checkpoint()
        checkpoint["queued"] = [completed.to_dict()]

        with self.assertRaisesRegex(ValueError, "repeats"):
            RecursiveFrontier.from_checkpoint(
                checkpoint, hypotheses_by_id=ledger.hypotheses_by_id()
            )

        forged = replace(completed, priority=0.1)
        self.assertFalse(frontier.reopen(forged, evidence_hash="evidence:v2", reason="forged"))
        self.assertFalse(frontier.push(completed))
        self.assertTrue(frontier.reopen(completed, evidence_hash="evidence:v2", reason="new evidence"))

    def test_checkpoint_round_trip_restores_pending_and_completed_identity(self):
        frontier = RecursiveFrontier()
        ledger = HypothesisLedger()
        hypothesis = ledger.create(
            "agent search closure is root",
            "record:decision",
            sample_defect_state(),
            seed_binding_identity="seed:round-trip",
        )
        completed = item_for(
            "record:completed", sample_defect_state("completed"), hypothesis, priority=0.95
        )
        pending = item_for("record:pending", sample_defect_state("pending"), hypothesis, priority=0.9)
        frontier.push(completed)
        frontier.push(pending)
        frontier.mark_completed(frontier.pop(), "evidence:completed")

        restored = RecursiveFrontier.from_checkpoint(
            frontier.checkpoint(), hypotheses_by_id=ledger.hypotheses_by_id()
        )

        self.assertEqual(restored.snapshot(), frontier.snapshot())
        self.assertFalse(
            restored.reopen(completed, evidence_hash="evidence:completed", reason="unchanged")
        )
        self.assertTrue(
            restored.reopen(completed, evidence_hash="evidence:new", reason="new evidence")
        )


if __name__ == "__main__":
    unittest.main()
