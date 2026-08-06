from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields
import unittest

from trace_attribution.candidate_budget import candidate_identity
from trace_attribution.candidate_clustering import (
    CandidateClusterManifest,
    build_candidate_cluster_manifest,
)
from trace_attribution.causal_state import CausalCandidate
from trace_attribution.cluster_triage import (
    CandidateClusterCoverageProof,
    CandidateClusterTriageDecision,
    CandidateClusterTriagePlan,
    CandidateClusterTriageRequest,
    build_candidate_cluster_triage_decision,
    build_candidate_cluster_triage_plan,
    build_candidate_cluster_triage_request,
    safe_build_candidate_cluster_triage_plan,
    _coverage_unsigned,
    _identity,
    _plan_unsigned_values,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import TraceNode


class SpoofSchemaKey:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        return str(other) == self.value


class ExplodingMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError("untrusted mapping access")

    def __iter__(self):
        raise RuntimeError("untrusted mapping iteration")

    def __len__(self):
        raise RuntimeError("untrusted mapping length")


def dataclass_values(value, **changes):
    output = {
        field.name: getattr(value, field.name)
        for field in fields(value)
    }
    output.update(changes)
    return output


def record(record_id, event_type, *, data=None, source_refs=()):
    return {
        "record_id": record_id,
        "component": "agent",
        "event_type": event_type,
        "title": record_id,
        "status": "completed",
        "timestamp": "2026-07-31T00:00:00Z",
        "data": dict(data or {}),
        "source_refs": list(source_refs),
    }


def candidate(graph, record_id, *, score=0.0, data_updates=None):
    ref = "record:{0}".format(record_id)
    node = graph.hydrate_node(ref)
    if data_updates:
        node = TraceNode(
            ref=node.ref,
            record_id=node.record_id,
            component=node.component,
            event_type=node.event_type,
            title=node.title,
            status=node.status,
            timestamp=node.timestamp,
            data={**node.data, **data_updates},
            source_refs=list(node.source_refs),
        )
    return CausalCandidate(
        ref=ref,
        node=node,
        source="attribution_edge",
        edge={"relation": "candidate_to_seed"},
        score=score,
        evidence_refs=(),
    )


class CandidateClusterTriageTest(unittest.TestCase):
    def setUp(self):
        self.trace = {
            "schema_version": "causal-ir/v1",
            "case_id": "triage-stage-a",
            "records": [
                record(
                    "action-plan",
                    "decision",
                    data={
                        "decision_type": "reasoning_block",
                        "action_group_id": "action-a",
                        "phase": "planning",
                        "causal_role": "business_dependency_role",
                    },
                ),
                record(
                    "action-result",
                    "tool.result",
                    data={
                        "action_group_id": "action-a",
                        "phase": "implementation",
                    },
                ),
                record(
                    "fallback-a",
                    "decision",
                    data={
                        "decision_type": "reasoning_block",
                        "file_path": "src/model.py",
                        "symbol": "Model.validate",
                    },
                ),
                record(
                    "fallback-b",
                    "decision",
                    data={
                        "decision_type": "reasoning_block",
                        "file_path": "src/model.py",
                        "symbol": "Model.validate",
                    },
                ),
                record("seed", "case.observed_defect"),
            ],
        }
        self.graph = TraceGraph.from_trace(self.trace)
        self.candidates = tuple(
            candidate(self.graph, record_id)
            for record_id in (
                "action-plan",
                "action-result",
                "fallback-a",
                "fallback-b",
            )
        )
        self.eligible = self.candidates[:3]
        self.paths = {
            value.ref: (value.ref, "record:seed")
            for value in self.candidates
        }
        self.manifest = self._manifest(self.candidates)
        self.request = build_candidate_cluster_triage_request(
            manifest=self.manifest,
            eligible_candidates=self.eligible,
        )

    def _manifest(self, candidates, *, labels=False):
        values = tuple(candidates)
        offered_refs = {value.ref for value in self.eligible}
        audit = [
            {
                "ref": value.ref,
                "discovered_rank": rank,
                "candidate_identity": candidate_identity(value),
                "disposition": (
                    "offered" if value.ref in offered_refs else "dropped"
                ),
                "reason": (
                    "input_order"
                    if value.ref in offered_refs
                    else "total_limit"
                ),
                **(
                    {"human_label": "root", "score": 0.99}
                    if labels
                    else {}
                ),
            }
            for rank, value in enumerate(values)
        ]
        return build_candidate_cluster_manifest(
            graph=self.graph,
            candidates=values,
            candidate_paths=self.paths,
            candidate_audit=audit,
            source_selection_identity="a" * 64,
            seed_ref="record:seed",
            defect_fingerprint="defect:omitted-validation",
        )

    def _decision(self, dispositions=None, *, rationale_suffix=""):
        dispositions = dispositions or {
            cluster_id: (
                "selected" if index == 0 else "uncertain"
            )
            for index, cluster_id in enumerate(self.request.cluster_ids)
        }
        return build_candidate_cluster_triage_decision(
            request=self.request,
            dispositions=dispositions,
            rationales={
                cluster_id: "grounded navigation {0}{1}".format(
                    cluster_id, rationale_suffix
                )
                for cluster_id in self.request.cluster_ids
            },
            evidence_refs={
                cluster_id: self.request.evidence_refs_for(cluster_id)[:1]
                for cluster_id in self.request.cluster_ids
            },
        )

    def test_request_contains_exactly_the_offered_original_candidates(self):
        self.assertEqual(
            set(self.request.eligible_candidate_refs),
            {value.ref for value in self.eligible},
        )
        self.assertNotIn(
            self.candidates[-1].ref,
            self.request.eligible_candidate_refs,
        )
        self.assertEqual(
            dict(
                zip(
                    self.request.eligible_candidate_refs,
                    self.request.eligible_candidate_identities,
                )
            ),
            {
                value.ref: candidate_identity(value)
                for value in self.eligible
            },
        )
        with self.assertRaisesRegex(ValueError, "offered"):
            build_candidate_cluster_triage_request(
                manifest=self.manifest,
                eligible_candidates=self.candidates,
            )

    def test_selected_and_uncertain_clusters_expand_all_eligible_original_members(self):
        decision = self._decision()
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision,
        )

        self.assertEqual(
            set(plan.effective_selected_cluster_ids),
            set(plan.selected_cluster_ids)
            | set(plan.uncertain_cluster_ids),
        )
        expected = {
            ref
            for cluster_id in plan.effective_selected_cluster_ids
            for ref in self.request.eligible_refs_for(cluster_id)
        }
        self.assertEqual(set(plan.expanded_candidate_refs), expected)
        self.assertEqual(
            len(plan.expanded_candidate_refs),
            len(set(plan.expanded_candidate_refs)),
        )
        self.assertTrue(plan.coverage_proof.partition_complete)
        self.assertTrue(plan.coverage_proof.expansion_complete)
        self.assertTrue(plan.coverage_proof.original_identity_only)

    def test_unselected_cluster_is_deferred_without_partial_member_expansion(self):
        cluster_ids = self.request.cluster_ids
        decision = self._decision(
            {
                cluster_id: (
                    "selected" if index == 0 else "unselected"
                )
                for index, cluster_id in enumerate(cluster_ids)
            }
        )
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision,
        )

        self.assertEqual(
            set(plan.expanded_candidate_refs),
            set(self.request.eligible_refs_for(cluster_ids[0])),
        )
        self.assertEqual(
            dict(plan.cluster_dispositions)[cluster_ids[-1]],
            "unselected_deferred",
        )

    def test_cluster_candidate_and_input_order_permutations_preserve_navigation_result(self):
        reversed_manifest = self._manifest(tuple(reversed(self.candidates)))
        reversed_request = build_candidate_cluster_triage_request(
            manifest=reversed_manifest,
            eligible_candidates=tuple(reversed(self.eligible)),
        )
        dispositions = {
            cluster_id: (
                "selected" if index == 0 else "uncertain"
            )
            for index, cluster_id in enumerate(sorted(self.request.cluster_ids))
        }
        baseline = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(dispositions),
        )
        reversed_decision = build_candidate_cluster_triage_decision(
            request=reversed_request,
            dispositions=dict(reversed(tuple(dispositions.items()))),
            rationales={
                cluster_id: "same facts"
                for cluster_id in reversed(reversed_request.cluster_ids)
            },
            evidence_refs={
                cluster_id: tuple(
                    reversed(reversed_request.evidence_refs_for(cluster_id))
                )[:1]
                for cluster_id in reversed_request.cluster_ids
            },
        )
        permuted = build_candidate_cluster_triage_plan(
            request=reversed_request,
            decision=reversed_decision,
        )

        self.assertEqual(
            baseline.effective_selected_cluster_ids,
            permuted.effective_selected_cluster_ids,
        )
        self.assertEqual(
            baseline.expanded_candidate_refs,
            permuted.expanded_candidate_refs,
        )
        self.assertEqual(
            baseline.expanded_candidate_identities,
            permuted.expanded_candidate_identities,
        )
        self.assertEqual(
            baseline.partition_identity,
            permuted.partition_identity,
        )

    def test_representatives_are_navigation_only_and_never_substitute_identity(self):
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        )
        original = dict(
            zip(
                self.request.eligible_candidate_refs,
                self.request.eligible_candidate_identities,
            )
        )
        cluster_identities = dict(self.request.cluster_content_identities)

        self.assertTrue(
            all(not ref.startswith("cluster:") for ref in plan.expanded_candidate_refs)
        )
        self.assertEqual(
            dict(
                zip(
                    plan.expanded_candidate_refs,
                    plan.expanded_candidate_identities,
                )
            ),
            {ref: original[ref] for ref in plan.expanded_candidate_refs},
        )
        self.assertTrue(
            set(plan.expanded_candidate_identities).isdisjoint(
                cluster_identities.values()
            )
        )

    def test_each_model_round_trips_with_exact_schema_and_is_tamper_evident(self):
        decision = self._decision()
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision,
        )
        models = (
            (CandidateClusterTriageRequest, self.request),
            (CandidateClusterTriageDecision, decision),
            (CandidateClusterCoverageProof, plan.coverage_proof),
            (CandidateClusterTriagePlan, plan),
        )
        for model_type, value in models:
            with self.subTest(model=model_type.__name__):
                payload = copy.deepcopy(value.to_dict())
                self.assertEqual(model_type.from_dict(payload), value)
                payload["unexpected"] = True
                with self.assertRaisesRegex(ValueError, "schema mismatch"):
                    model_type.from_dict(payload)

        tampered = plan.to_dict()
        tampered["expanded_candidate_refs"].pop()
        with self.assertRaisesRegex(ValueError, "identity|expansion|align"):
            CandidateClusterTriagePlan.from_dict(tampered)

    def test_models_are_deeply_immutable_at_the_public_boundary(self):
        with self.assertRaises(FrozenInstanceError):
            self.request.seed_ref = "record:other"
        with self.assertRaises(TypeError):
            self.request.cluster_directory[0]["cluster_id"] = "cluster:v1:forged"

    def test_malformed_or_stale_manifest_falls_back_to_all_eligible_candidates(self):
        decision = self._decision()
        malformed = self.manifest.to_dict()
        malformed["candidate_facts"][0]["candidate_identity"] = "b" * 64
        malformed_plan = safe_build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision,
            manifest=malformed,
        )
        stale = self.manifest.to_dict()
        stale["source_selection_identity"] = "b" * 64
        stale["manifest_identity"] = "c" * 64
        stale_plan = safe_build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision,
            manifest=stale,
        )

        for plan in (malformed_plan, stale_plan):
            self.assertTrue(plan.is_fallback)
            self.assertEqual(
                plan.expanded_candidate_refs,
                self.request.eligible_candidate_refs,
            )
            self.assertEqual(
                set(dict(plan.cluster_dispositions).values()),
                {"fallback_full_paging"},
            )

    def test_incomplete_or_missing_evidence_decision_falls_back_to_full_paging(self):
        incomplete = self._decision().to_dict()
        incomplete["decisions"].pop()
        missing_evidence = self._decision().to_dict()
        missing_evidence["decisions"][0]["evidence_refs"] = []

        for decision in (incomplete, missing_evidence):
            with self.subTest(decision=decision):
                plan = safe_build_candidate_cluster_triage_plan(
                    request=self.request,
                    decision=decision,
                    manifest=self.manifest,
                )
                self.assertTrue(plan.is_fallback)
                self.assertEqual(
                    set(plan.expanded_candidate_refs),
                    set(self.request.eligible_candidate_refs),
                )

    def test_safe_fallback_does_not_traverse_recursive_or_non_string_decisions(self):
        recursive = {}
        recursive["decisions"] = recursive
        malformed_decisions = (
            recursive,
            42,
            {1: "non-string-key"},
            ExplodingMapping(),
        )

        for decision in malformed_decisions:
            with self.subTest(decision_type=type(decision).__name__):
                plan = safe_build_candidate_cluster_triage_plan(
                    request=self.request,
                    decision=decision,
                    manifest=self.manifest,
                )
                self.assertTrue(plan.is_fallback)
                self.assertEqual(
                    plan.expanded_candidate_refs,
                    self.request.eligible_candidate_refs,
                )

        plan = safe_build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
            manifest=ExplodingMapping(),
        )
        self.assertTrue(plan.is_fallback)
        self.assertEqual(
            plan.expanded_candidate_refs,
            self.request.eligible_candidate_refs,
        )

    def test_public_sequence_inputs_are_copied_before_identity_validation(self):
        request_refs = list(self.request.eligible_candidate_refs)
        copied_request = CandidateClusterTriageRequest(
            **dataclass_values(
                self.request,
                eligible_candidate_refs=request_refs,
            )
        )
        request_refs.append("record:foreign")

        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        )
        proof_refs = list(plan.coverage_proof.expanded_candidate_refs)
        copied_proof = CandidateClusterCoverageProof(
            **dataclass_values(
                plan.coverage_proof,
                expanded_candidate_refs=proof_refs,
            )
        )
        proof_refs.append("record:foreign")
        plan_refs = list(plan.expanded_candidate_refs)
        copied_plan = CandidateClusterTriagePlan(
            **dataclass_values(plan, expanded_candidate_refs=plan_refs)
        )
        plan_refs.append("record:foreign")

        self.assertEqual(
            copied_request.eligible_candidate_refs,
            self.request.eligible_candidate_refs,
        )
        self.assertEqual(
            copied_proof.expanded_candidate_refs,
            plan.coverage_proof.expanded_candidate_refs,
        )
        self.assertEqual(
            copied_plan.expanded_candidate_refs,
            plan.expanded_candidate_refs,
        )

    def test_plan_rejects_a_self_consistent_proof_with_different_cluster_partition(self):
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        )
        proof = plan.coverage_proof
        swapped_selected = proof.unselected_cluster_ids
        swapped_unselected = proof.selected_cluster_ids
        proof_unsigned = _coverage_unsigned(
            mode=proof.mode,
            request_identity=proof.request_identity,
            selection_identity=proof.selection_identity,
            eligible_set_identity=proof.eligible_set_identity,
            partition_identity=proof.partition_identity,
            selected_cluster_ids=swapped_selected,
            unselected_cluster_ids=swapped_unselected,
            expanded=tuple(
                zip(
                    proof.expanded_candidate_refs,
                    proof.expanded_candidate_identities,
                )
            ),
            expanded_set_identity=proof.expanded_set_identity,
            partition_complete=True,
            expansion_complete=True,
            original_identity_only=True,
        )
        forged_proof = CandidateClusterCoverageProof(
            **dataclass_values(
                proof,
                selected_cluster_ids=swapped_selected,
                unselected_cluster_ids=swapped_unselected,
                proof_identity=_identity(
                    "candidate-cluster-coverage-proof-identity/v1",
                    proof_unsigned,
                ),
            )
        )
        values = dataclass_values(
            plan,
            coverage_proof=forged_proof,
        )
        values["plan_identity"] = _identity(
            "candidate-cluster-triage-plan-identity/v1",
            _plan_unsigned_values(values),
        )

        with self.assertRaisesRegex(ValueError, "coverage proof.*cluster"):
            CandidateClusterTriagePlan(**values)

    def test_plan_rejects_representative_outside_its_complete_cluster_membership(self):
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        )
        representatives = [
            copy.deepcopy(dict(item)) for item in plan.representative_refs
        ]
        representatives[0]["refs"] = ["record:foreign"]
        values = dataclass_values(
            plan,
            representative_refs=representatives,
        )
        values["plan_identity"] = _identity(
            "candidate-cluster-triage-plan-identity/v1",
            _plan_unsigned_values(values),
        )

        with self.assertRaisesRegex(ValueError, "representative.*member"):
            CandidateClusterTriagePlan(**values)

    def test_exact_schema_rejects_string_equivalent_non_string_keys(self):
        payload = self.request.to_dict()
        schema = payload.pop("schema")
        payload[SpoofSchemaKey("schema")] = schema

        with self.assertRaisesRegex(ValueError, "non-string|schema mismatch"):
            CandidateClusterTriageRequest.from_dict(payload)

    def test_tampered_request_decision_and_plan_identities_fail_closed(self):
        request_payload = self.request.to_dict()
        request_payload["manifest_identity"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "identity"):
            CandidateClusterTriageRequest.from_dict(request_payload)

        decision_payload = self._decision().to_dict()
        decision_payload["decisions"][0]["disposition"] = "unselected"
        with self.assertRaisesRegex(ValueError, "identity"):
            CandidateClusterTriageDecision.from_dict(decision_payload)

        plan_payload = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        ).to_dict()
        plan_payload["cluster_dispositions"][0]["disposition"] = (
            "unselected_deferred"
        )
        with self.assertRaisesRegex(ValueError, "identity|disposition"):
            CandidateClusterTriagePlan.from_dict(plan_payload)

    def test_labels_scores_and_rationale_do_not_control_selection_identity(self):
        relabeled_candidates = tuple(
            candidate(
                self.graph,
                value.ref.split(":", 1)[1],
                score=0.99 - index / 100,
                data_updates={
                    "human_labels": {"root": index == 2},
                    "benchmarkScore": 100 - index,
                    "BENCHMARK_SCORE": 90 - index,
                    "BenchmarkScore": 80 - index,
                },
            )
            for index, value in enumerate(self.candidates)
        )
        relabeled_manifest = self._manifest(relabeled_candidates, labels=True)
        relabeled_request = build_candidate_cluster_triage_request(
            manifest=relabeled_manifest,
            eligible_candidates=relabeled_candidates[:3],
        )
        decision_a = self._decision(rationale_suffix=" A")
        decision_b = self._decision(rationale_suffix=" B")
        plan_a = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision_a,
        )
        plan_b = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=decision_b,
        )
        relabeled_decision = build_candidate_cluster_triage_decision(
            request=relabeled_request,
            dispositions={
                item["cluster_id"]: item["disposition"]
                for item in decision_a.decisions
            },
            rationales={
                item["cluster_id"]: item["rationale"]
                for item in decision_a.decisions
            },
            evidence_refs={
                item["cluster_id"]: tuple(item["evidence_refs"])
                for item in decision_a.decisions
            },
        )
        relabeled_plan = build_candidate_cluster_triage_plan(
            request=relabeled_request,
            decision=relabeled_decision,
        )

        self.assertEqual(relabeled_manifest.manifest_identity, self.manifest.manifest_identity)
        self.assertEqual(relabeled_request.request_identity, self.request.request_identity)
        self.assertEqual(
            relabeled_request.eligible_set_identity,
            self.request.eligible_set_identity,
        )
        self.assertEqual(
            relabeled_request.partition_identity,
            self.request.partition_identity,
        )
        self.assertEqual(decision_a.selection_identity, decision_b.selection_identity)
        self.assertNotEqual(decision_a.judgment_identity, decision_b.judgment_identity)
        self.assertEqual(plan_a.expanded_candidate_refs, plan_b.expanded_candidate_refs)
        self.assertEqual(plan_a.selection_identity, plan_b.selection_identity)
        self.assertNotEqual(plan_a.plan_identity, plan_b.plan_identity)
        self.assertEqual(relabeled_decision.selection_identity, decision_a.selection_identity)
        self.assertEqual(relabeled_plan.plan_identity, plan_a.plan_identity)

    def test_ordinary_business_role_field_is_preserved_as_candidate_fact_not_a_verdict(self):
        fact = next(
            value
            for value in self.manifest.candidate_facts
            if value.ref == "record:action-plan"
        )
        self.assertEqual(fact.action_identity, "action_group_id:action-a")
        plan = build_candidate_cluster_triage_plan(
            request=self.request,
            decision=self._decision(),
        )
        self.assertIn("record:action-plan", plan.expanded_candidate_refs)


if __name__ == "__main__":
    unittest.main()
