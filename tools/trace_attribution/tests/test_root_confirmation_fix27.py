from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    ClaudeCausalJudge,
)
from trace_attribution.causal_state import (
    CausalCandidate,
    DefectState,
    LocalStateOwner,
    RecursiveAttributionReport,
    annotate_report_semantic_anchors,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.errors import (
    JudgeProviderError,
    TransportCallError,
    TransportCallResult,
)
from trace_attribution.evidence_capsule import (
    CAPSULE_SCHEMA_VERSION,
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
)
from trace_attribution import evidence_capsule
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _confirmation_request_identity,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_global_judge import (
    payload as global_payload,
    sample_request,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    artifact_state,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    SelectiveGlobalJudge,
    checkpoint_for,
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    STARTS,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
)


def formal_manifest(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "run_id": "fix27-run",
        "subject_revision": "git:active",
        "subject_revision_provenance": {
            "method": "case_trace_config",
            "source": "CaseTraceConfig.subjectRevision",
            "bound_at": "case_start",
            "case_id": case_id,
            "run_id": "fix27-run",
        },
    }


def artifact_graph(
    root: Path,
    owners: tuple[tuple[str, str], ...],
) -> TraceGraph:
    content = b"fix27 strict artifact evidence"
    case_id = "fix27-artifact-envelope"
    trace = {
        "case_id": case_id,
        "manifest": formal_manifest(case_id),
        "artifacts": [
            {
                "artifact_id": "proof",
                "kind": "text",
                "path": "artifacts/proof.txt",
                "hash": "sha256:{0}".format(
                    hashlib.sha256(content).hexdigest()
                ),
                "byte_length": len(content),
            }
        ],
        "records": [],
    }
    for record_id, binding in owners:
        data = {"summary": "{0} artifact owner".format(binding)}
        if binding == "active":
            data.update(
                {
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                }
            )
        elif binding == "stale":
            data.update(
                {
                    "subject_revision": "git:stale",
                    "revision_provenance_status": "valid",
                }
            )
        trace["records"].append(
            {
                "record_id": record_id,
                "component": "agent",
                "event_type": "decision",
                "artifact_refs": (
                    ["artifact:proof"]
                    if binding in {"active", "unbound", "stale"}
                    else []
                ),
                "data": data,
            }
        )
    target = root / "artifacts" / "proof.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return TraceGraph.from_trace(trace, artifact_root=root)


def artifact_capsule(graph: TraceGraph, candidate_ref: str):
    defect = DefectState.create(
        label="strict_artifact_owner",
        expected="Only owner-bound artifact evidence is exposed.",
        actual="Artifact evidence requires strict owner validation.",
        mechanism="A candidate artifact must bind to its active owner.",
        scope="task_quality",
    )
    candidate = CausalCandidate(
        ref=candidate_ref,
        node=graph.nodes[candidate_ref],
        source="progress_window",
        evidence_refs=(candidate_ref,),
    )
    return build_candidate_evidence_capsules(
        graph=graph,
        candidates=(candidate,),
        defect_state=defect,
        downstream_paths={candidate_ref: (candidate_ref,)},
        start_refs=(candidate_ref,),
    )[0]


def no_defect_payload_from_prompt(prompt: str) -> dict:
    request = json.loads(prompt)["request"]
    capsules = request["candidate_evidence_capsules"]
    eligible = [
        item["candidate_ref"]
        for item in capsules
        if item["candidate"]["root_candidate_eligible"] is True
    ]
    assessments = []
    for index, capsule in enumerate(capsules):
        candidate_ref = capsule["candidate_ref"]
        assessments.append(
            {
                "candidate_ref": candidate_ref,
                "defect_status": "absent",
                "input_defect_status": (
                    "absent" if candidate_ref in eligible else "unknown"
                ),
                "output_defect_status": "absent",
                "causal_path_refs": capsule["downstream_path"],
                "counterfactual": {
                    "intervention_ref": candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                "compared_candidate_refs": eligible,
                "causal_role": (
                    "exculpatory_evidence" if index == 0 else "unrelated"
                ),
                "reason": "Grounded evidence refutes the active defect.",
                "evidence_refs": [candidate_ref],
                "confidence": 0.9,
            }
        )
    return {
        "outcome": "no_defect",
        "reason": "Grounded evidence refutes the active defect.",
        "assessments": assessments,
        "selected_candidate_refs": [],
        "expansion_requests": [],
        "decisive_evidence_refs": [capsules[0]["candidate_ref"]],
        "missing_evidence": [],
        "confidence": 0.9,
        "active_focus_binding": {
            "seed_ref": request["seed_ref"],
            "defect_fingerprint": request["active_defect"]["fingerprint"],
            "active_focus_text_hash": request["active_focus_text_hash"],
        },
    }


class StaticTransport:
    model = "fix27-model"
    max_tokens = 4096
    repair_max_tokens = 1024
    thinking_config = None

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def create_message_text_with_usage(self, *, system, messages, max_tokens):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return TransportCallResult(str(value), physical_requests=1)


class RealGlobalJudgeTypedFailureTest(unittest.TestCase):
    def test_provider_failure_is_typed_and_preserves_physical_count(self):
        transport = StaticTransport(
            [
                TransportCallError(
                    JudgeProviderError("fix27 provider unavailable"),
                    physical_requests=1,
                )
            ]
        )
        with self.assertRaisesRegex(
            BoundedJudgeCallError,
            "provider_error.*fix27 provider unavailable",
        ) as raised:
            ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(),
            ).judge_candidates_bounded(
                sample_request(),
                max_physical_requests=3,
            )
        self.assertEqual(raised.exception.physical_requests, 1)

    def test_zero_remaining_budget_is_typed_without_transport(self):
        transport = StaticTransport([])
        with self.assertRaisesRegex(
            BoundedJudgeCallError,
            "request_budget_exhausted",
        ) as raised:
            ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(),
            ).judge_candidates_bounded(
                sample_request(),
                max_physical_requests=0,
            )
        self.assertEqual(raised.exception.physical_requests, 0)
        self.assertEqual(transport.calls, 0)

    def test_invalid_payload_after_repair_is_typed_with_exact_count(self):
        transport = StaticTransport(["{}", "{}", "{}"])
        with self.assertRaisesRegex(
            BoundedJudgeCallError,
            "validation_error.*full retry invalid",
        ) as raised:
            ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(),
            ).judge_candidates_bounded(
                sample_request(),
                max_physical_requests=3,
            )
        self.assertEqual(raised.exception.physical_requests, 3)
        self.assertEqual(transport.calls, 3)

    def test_valid_payload_remains_a_successful_bounded_result(self):
        request = sample_request()
        transport = StaticTransport(
            [json.dumps(global_payload(outcome="candidate_roots", request=request))]
        )
        result = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        ).judge_candidates_bounded(
            request,
            max_physical_requests=1,
        )
        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(result.value.outcome, "candidate_roots")

    def test_real_adapter_failure_uses_canonical_seed_failure_and_other_seed_progresses(
        self,
    ):
        class SelectiveTransport:
            model = "fix27-selective"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = 0

            def create_message_text_with_usage(
                self, *, system, messages, max_tokens
            ):
                self.calls += 1
                request = json.loads(messages[0]["content"])["request"]
                if request["seed_ref"] == "record:seed_one":
                    raise TransportCallError(
                        JudgeProviderError("seed one provider failure"),
                        physical_requests=1,
                    )
                return TransportCallResult(
                    json.dumps(
                        no_defect_payload_from_prompt(messages[0]["content"])
                    ),
                    physical_requests=1,
                )

        report = AgenticRecursiveAnalyzer(
            judge=ClaudeCausalJudge(
                transport=SelectiveTransport(),
                cache=JudgmentCache(),
            ),
            fusion_mode="retrieval-global",
            max_judge_requests=8,
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(by_ref["record:seed_one"].outcome, "evidence_gap")
        self.assertEqual(by_ref["record:seed_two"].outcome, "no_defect")
        failures = [
            item
            for item in report.investigation_journal
            if item.get("status") == "failed"
        ]
        completed = [
            item
            for item in report.investigation_journal
            if item.get("status") == "completed"
        ]
        episodes = [
            item
            for item in report.metadata["unresolved_branches"]
            if item.get("global_pass_identity")
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(failures[0]["physical_request_delta"], 1)
        self.assertEqual(report.metadata["global_judge_physical_request_count"], 2)


class StrictGlobalArtifactHydrationTest(unittest.TestCase):
    def test_unbound_and_stale_owners_never_expose_content_and_report_gap(self):
        for binding in ("unbound", "stale"):
            with self.subTest(binding=binding), tempfile.TemporaryDirectory() as directory:
                graph = artifact_graph(
                    Path(directory),
                    (("candidate", binding),),
                )
                manifest = graph.artifact_hydration_manifest(
                    "record:candidate"
                )
                self.assertEqual(manifest["hydrated_artifacts"], [])
                self.assertEqual(
                    manifest["ineligible_artifact_evidence"][0][
                        "owner_ref"
                    ],
                    "record:candidate",
                )
                self.assertNotIn(
                    "fix27 strict artifact evidence",
                    stable_json(manifest),
                )

    def test_ambiguous_owner_requires_explicit_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("owner_one", "active"), ("owner_two", "active")),
            )
            with self.assertRaisesRegex(ValueError, "ambiguous|owner"):
                graph.artifact_evidence_envelope(
                    "artifact:proof",
                    fact_kind="global_candidate_artifact",
                )
            manifest = graph.artifact_hydration_manifest("record:owner_one")
            self.assertEqual(
                manifest["hydrated_artifacts"][0]["owner_reference"][
                    "resolved_ref"
                ],
                "record:owner_one",
            )

    def test_valid_capsule_uses_complete_owner_envelope_and_rejects_substitution(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("candidate", "active"),),
            )
            capsule = artifact_capsule(graph, "record:candidate")
            hydrated = capsule.to_dict()["artifact_hydration"][
                "hydrated_artifacts"
            ][0]
            self.assertTrue(hydrated.get("owner_binding_identity"))
            self.assertEqual(
                hydrated["owner_reference"]["resolved_ref"],
                "record:candidate",
            )
            self.assertEqual(hydrated["byte_range"], [0, hydrated["byte_count"]])
            self.assertTrue(hydrated["content_hash"].startswith("sha256:"))
            self.assertEqual(
                capsule.judge_dict()["artifact_hydration"][
                    "hydrated_artifacts"
                ][0],
                hydrated,
            )

            substituted = capsule.to_dict()
            substituted["artifact_hydration"]["hydrated_artifacts"][0][
                "owner_reference"
            ]["resolved_ref"] = "record:substitute"
            substituted["validation_source"][
                "prompt_collections_sha256"
            ] = evidence_capsule._prompt_collections_sha256(
                retrieval_edge=substituted["candidate"]["retrieval_edge"],
                action_group=substituted["action_group"],
                evidence_references=substituted["evidence_references"],
                artifact_hydration=substituted["artifact_hydration"],
                missing_evidence_refs=substituted["missing_evidence_refs"],
            )
            restored = CandidateEvidenceCapsule.from_dict(substituted)
            with self.assertRaisesRegex(
                ValueError,
                "artifact|owner|prompt-bearing",
            ):
                from trace_attribution.evidence_capsule import (
                    validate_candidate_evidence_capsule_against_graph,
                )

                validate_candidate_evidence_capsule_against_graph(
                    graph,
                    restored,
                    authoritative_candidates=(
                        CausalCandidate(
                            ref="record:candidate",
                            node=graph.nodes["record:candidate"],
                            source="progress_window",
                            evidence_refs=("record:candidate",),
                        ),
                    ),
                )

    def test_rejected_capsule_hydration_is_judge_visible_as_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("candidate", "unbound"),),
            )
            capsule = artifact_capsule(graph, "record:candidate")
            judge_payload = capsule.judge_dict()
            self.assertNotIn(
                "fix27 strict artifact evidence",
                stable_json(judge_payload),
            )
            self.assertEqual(
                judge_payload["artifact_evidence_gaps"][0]["status"],
                "owner_ineligible",
            )
            self.assertIn(
                "artifact:proof",
                capsule.missing_evidence_refs,
            )

    def test_capsule_validation_envelope_and_graph_rebuild_are_identical(self):
        from trace_attribution.global_judge import (
            GlobalCandidateJudgeRequest,
            active_focus_text_sha256,
            global_candidate_request_from_validation_envelope,
            validate_global_candidate_request_against_graph,
        )

        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("candidate", "active"),),
            )
            capsule = artifact_capsule(graph, "record:candidate")
            request = GlobalCandidateJudgeRequest(
                case_id=graph.case_id,
                objective="Inspect strict artifact evidence.",
                analysis_perspective="",
                seed_ref="record:candidate",
                active_defect=capsule.defect_state,
                active_focus_text=capsule.defect_state.actual,
                active_focus_text_hash=active_focus_text_sha256(
                    capsule.defect_state.actual
                ),
                start_refs=("record:candidate",),
                capsules=(capsule,),
            )
            restored = global_candidate_request_from_validation_envelope(
                request.validation_envelope(),
                graph=graph,
                authoritative_candidates=(
                    CausalCandidate(
                        ref="record:candidate",
                        node=graph.nodes["record:candidate"],
                        source="progress_window",
                        evidence_refs=("record:candidate",),
                    ),
                ),
            )
            validate_global_candidate_request_against_graph(
                graph,
                restored,
                authoritative_candidates=(
                    CausalCandidate(
                        ref="record:candidate",
                        node=graph.nodes["record:candidate"],
                        source="progress_window",
                        evidence_refs=("record:candidate",),
                    ),
                ),
            )
            self.assertEqual(restored, request)
            self.assertEqual(
                restored.capsules[0].artifact_hydration,
                capsule.artifact_hydration,
            )

    def test_checkpoint_report_and_evaluator_rebuild_strict_artifact_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = shared_root_trace()
            trace["manifest"] = formal_manifest(trace["case_id"])
            for record in trace["records"]:
                record.setdefault("data", {}).update(
                    {
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    }
                )
            content = b"fix27 report artifact evidence"
            trace["artifacts"] = [
                {
                    "artifact_id": "proof",
                    "kind": "text",
                    "path": "artifacts/proof.txt",
                    "hash": "sha256:{0}".format(
                        hashlib.sha256(content).hexdigest()
                    ),
                    "byte_length": len(content),
                }
            ]
            trace["records"][0]["artifact_refs"] = ["artifact:proof"]
            target = root / "artifacts" / "proof.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            graph = TraceGraph.from_trace(trace, artifact_root=root)
            starts = ("record:seed_one",)
            config = shared_root_checkpoint_config(trace, OBJECTIVE, starts)
            checkpoint_root = root / "strict-artifact.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
            checkpoint = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )
            restored_state = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(
                    checkpoint,
                    list(checkpoint.actions),
                ),
            )
            expected_manifest = graph.artifact_hydration_manifest(
                "record:shared_root"
            )
            for seed in restored_state.seed_results():
                capsules = seed.to_dict()["global_judgment"][
                    "validation_envelope"
                ]["candidate_evidence_capsules"]
                shared_capsule = next(
                    item
                    for item in capsules
                    if item["candidate_ref"] == "record:shared_root"
                )
                self.assertEqual(
                    shared_capsule["artifact_hydration"],
                    expected_manifest,
                )

            payload = report.to_dict()
            restored_report = RecursiveAttributionReport.from_dict(payload)
            validate_recursive_report_against_graph(
                graph,
                restored_report,
                label="fix27 strict artifact report",
                action_records=checkpoint.actions,
            )
            compare_report(
                annotate_report_semantic_anchors(
                    graph.case_id,
                    graph.nodes,
                    payload,
                    graph=graph,
                ),
                permissive_labels(graph.case_id),
                None,
                graph=graph,
            )


class GlobalFailureProjectionBijectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.tempdir,
            cls.checkpoint_root,
            cls.config,
            cls.checkpoint,
            cls.report,
        ) = checkpoint_for(
            shared_root_trace(),
            SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
            name="fix27-global-failure",
        )
        cls.graph = TraceGraph.from_trace(shared_root_trace())

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def assert_checkpoint_rejects(self, mutation):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        mutation(latest_snapshot(actions))
        with self.assertRaisesRegex(
            ValueError,
            "failure|global|episode|seed|bijection|projection",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(self.checkpoint, actions),
            )

    def assert_report_and_evaluator_reject(self, mutation):
        payload = self.report.to_dict()
        mutation(payload["metadata"], payload["seed_results"])
        try:
            restored = RecursiveAttributionReport.from_dict(payload)
        except ValueError as exc:
            self.assertRegex(
                str(exc),
                "failure|global|episode|seed|bijection|projection",
            )
        else:
            with self.assertRaisesRegex(
                ValueError,
                "failure|global|episode|seed|bijection|projection",
            ):
                validate_recursive_report_against_graph(
                    self.graph,
                    restored,
                    label="fix27 failure report",
                    action_records=self.checkpoint.actions,
                )
        with self.assertRaises(EvaluationSafetyError):
            compare_report(
                payload,
                permissive_labels(self.graph.case_id),
                None,
                graph=self.graph,
            )

    def test_checkpoint_rejects_mutated_detail_duplicate_episode_and_owner(self):
        def failed_action(snapshot):
            return next(
                item
                for item in snapshot["investigation_journal"]
                if item.get("status") == "failed"
            )

        def failed_episode(snapshot):
            return next(
                item
                for item in snapshot["unresolved_branches"]
                if item.get("global_pass_identity")
            )

        def substitute_owner_everywhere(snapshot):
            action = failed_action(snapshot)
            episode = failed_episode(snapshot)
            substituted = LocalStateOwner.create(
                seed_binding_identity=action["seed_binding_identity"],
                hypothesis_id="hyp:coordinated-substitution",
                visit_key="visit:coordinated-substitution",
                occurrence_key="global_candidate_pass",
            ).to_dict()
            action["owner"] = copy.deepcopy(substituted)
            action["failure_projection"]["owner"] = copy.deepcopy(
                substituted
            )
            episode["owner"] = copy.deepcopy(substituted)
            episode["failure_projection"]["owner"] = copy.deepcopy(
                substituted
            )

        mutations = {
            "detail": lambda snapshot: failed_episode(snapshot).update(
                {"details": "mutated diagnostic"}
            ),
            "duplicate": lambda snapshot: snapshot[
                "unresolved_branches"
            ].append(copy.deepcopy(failed_episode(snapshot))),
            "missing": lambda snapshot: snapshot[
                "unresolved_branches"
            ].remove(failed_episode(snapshot)),
            "owner": lambda snapshot: failed_episode(snapshot)[
                "owner"
            ].update({"hypothesis_id": "hyp:substituted"}),
            "coordinated owner": substitute_owner_everywhere,
            "request count": lambda snapshot: failed_action(snapshot).update(
                {"physical_request_delta": 7}
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(mutation=label):
                self.assert_checkpoint_rejects(mutation)

    def test_report_and_evaluator_reject_seed_gap_or_episode_drift(self):
        def mutate_seed(metadata, seeds):
            seed = next(
                item
                for item in seeds
                if item["start_ref"] == "record:seed_one"
            )
            seed["blocking_reasons"].append("extra_seed_gap")

        def mutate_episode(metadata, seeds):
            episode = next(
                item
                for item in metadata["unresolved_branches"]
                if item.get("global_pass_identity")
            )
            episode["failure_projection"] = copy.deepcopy(
                metadata["global_candidate_failures"][0]
            )
            episode["failure_projection"]["detail"] = "mutated diagnostic"

        def duplicate_seed_gap(metadata, seeds):
            seed = next(
                item
                for item in seeds
                if item["start_ref"] == "record:seed_one"
            )
            seed["missing_evidence"].append(seed["missing_evidence"][0])

        for mutation in (mutate_seed, mutate_episode, duplicate_seed_gap):
            with self.subTest(mutation=mutation.__name__):
                self.assert_report_and_evaluator_reject(mutation)

    def test_two_independent_failed_seeds_keep_two_exact_projections(self):
        from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
            FailingGlobalJudge,
        )

        report = AgenticRecursiveAnalyzer(
            judge=FailingGlobalJudge("bounded"),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        failures = report.metadata["global_candidate_failures"]
        episodes = [
            item["failure_projection"]
            for item in report.metadata["unresolved_branches"]
            if item.get("failure_projection")
        ]
        self.assertEqual(len(failures), 2)
        self.assertCountEqual(failures, episodes)
        self.assertEqual(
            {item["seed_binding_identity"] for item in failures},
            {item.seed_binding_identity for item in report.seed_results},
        )


class CanonicalPendingConfirmationIdentityTest(unittest.TestCase):
    def queued_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return artifact_state(Path(tempdir.name))

    def test_enqueue_rejects_arbitrary_and_extra_identity_fields(self):
        for mutation in ("arbitrary", "extra"):
            with self.subTest(mutation=mutation):
                _, state, _, queued = self.queued_state()
                if mutation == "arbitrary":
                    queued["semantic_identity"] = "a" * 64
                else:
                    queued["semantic_identity_alias"] = queued[
                        "semantic_identity"
                    ]
                with self.assertRaisesRegex(
                    ValueError,
                    "semantic|identity|schema|extra|missing",
                ):
                    state.enqueue_confirmation(queued)

    def test_enqueue_canonically_populates_a_missing_request_identity(self):
        _, state, _, queued = self.queued_state()
        queued.pop("semantic_identity")
        self.assertTrue(state.enqueue_confirmation(queued))
        self.assertTrue(
            state.confirmation_queue[0]["semantic_identity"].startswith(
                "confirmation_request:v2:"
            )
        )

    def test_live_queue_key_projection_recomputes_canonical_identity(self):
        _, state, _, queued = self.queued_state()
        self.assertTrue(state.enqueue_confirmation(queued))
        state.confirmation_queue[0]["semantic_identity"] = "f" * 64
        with self.assertRaisesRegex(
            ValueError,
            "semantic|identity|canonical",
        ):
            state.validate_confirmation_queue_bound()

    def test_checkpoint_report_and_evaluator_reject_identity_substitution(self):
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
            name="fix27-confirmation-identity",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())

        actions = copy.deepcopy(list(checkpoint.actions))
        snapshot = latest_snapshot(actions)
        snapshot["confirmation_queue"][0]["semantic_identity"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "semantic|identity|canonical"):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

        payload = report.to_dict()
        payload["metadata"]["confirmation_queue"][0][
            "semantic_identity"
        ] = "e" * 64
        restored = RecursiveAttributionReport.from_dict(payload)
        with self.assertRaisesRegex(ValueError, "semantic|identity|canonical"):
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="fix27 canonical confirmation identity",
                action_records=checkpoint.actions,
            )
        with self.assertRaises(EvaluationSafetyError):
            compare_report(
                payload,
                permissive_labels(graph.case_id),
                None,
                graph=graph,
            )

    def test_canonical_identity_is_the_provider_action_and_replay_key(self):
        judge = SelectiveGlobalJudge()
        tempdir, checkpoint_root, config, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            judge,
            fusion_mode="retrieval-global",
            name="fix27-confirmation-replay",
        )
        self.addCleanup(tempdir.cleanup)
        queued = report.metadata["confirmation_queue"][0]
        expected = _confirmation_request_identity(
            next(
                request
                for request in judge.confirmation_requests
                if request.hypothesis_id == queued["hypothesis_id"]
            )
        )
        action = next(
            item
            for item in checkpoint.actions
            if item.get("operation") in {
                "confirmation_completed",
                "confirmation_failed",
            }
            and item.get("semantic_key") == "confirmation:{0}".format(expected)
        )
        self.assertEqual(queued["semantic_identity"], expected)
        self.assertEqual(
            action["payload"]["action_projection"]["request_identity"],
            expected,
        )

        class NoProviderReplayJudge(SelectiveGlobalJudge):
            def confirm_candidate_bounded(
                self, request, *, max_physical_requests
            ):
                raise AssertionError("valid replay must not call confirmation provider")

        replayed = AgenticRecursiveAnalyzer(
            judge=NoProviderReplayJudge(),
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        self.assertEqual(replayed.to_dict(), report.to_dict())


class Fix27VersionIdentityTest(unittest.TestCase):
    def test_changed_judge_visible_and_persistence_meanings_are_versioned(self):
        from scripts.evaluate_recursive_attribution import (
            REPORT_SCHEMA_VERSION,
        )
        from trace_attribution.causal_state import (
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            MODERN_REPORT_SCHEMA_VERSION,
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        from trace_attribution.checkpoint import CHECKPOINT_SCHEMA_VERSION
        from trace_attribution.global_judge import (
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
        )
        from trace_attribution.graph import (
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        )

        self.assertEqual(CAPSULE_SCHEMA_VERSION, "candidate-evidence-capsule/v7")
        self.assertEqual(
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            "global-candidate-judgment/v7",
        )
        self.assertEqual(
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
            "graph-external-evidence-eligibility/v5",
        )
        self.assertIn(
            "failure-projection/v4",
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "confirmation-request-identity/v2",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(MODERN_REPORT_SCHEMA_VERSION, "recursive-attribution-report/v13")
        self.assertEqual(REPORT_SCHEMA_VERSION, MODERN_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v12",
        )
        self.assertEqual(ACTION_STATE_SCHEMA, "recursive-analysis-actions/v10")

    def test_old_fix26_report_and_capsule_identities_are_rejected(self):
        capsule = sample_request().capsules[0].to_dict()
        capsule["schema_version"] = "candidate-evidence-capsule/v6"
        with self.assertRaisesRegex(ValueError, "schema"):
            CandidateEvidenceCapsule.from_dict(capsule)

        report = AgenticRecursiveAnalyzer(
            judge=SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        ).to_dict()
        report["schema_version"] = "recursive-attribution-report/v9"
        with self.assertRaisesRegex(ValueError, "schema|unsupported"):
            RecursiveAttributionReport.from_dict(report)


if __name__ == "__main__":
    unittest.main()
