import unittest

from trace_attribution.candidate_episode import (
    classify_episode_role,
    extract_episode_key,
    select_episode_diverse_reserve,
)


def candidate(
    ref,
    *,
    position,
    decision_type="reasoning_block",
    phase="",
    episode_id="",
    event_type="decision",
    chosen_action="",
    extra_data=None,
):
    data = {
        "decision_type": decision_type,
        "phase": phase,
        "episode_id": episode_id,
        "chosen_action": chosen_action,
        **(extra_data or {}),
    }
    return {
        "ref": ref,
        "position": position,
        "node": {
            "ref": ref,
            "event_type": event_type,
            "data": data,
        },
        "edge": {},
    }


def path_node(ref, event_type, **data):
    return {
        "ref": ref,
        "event_type": event_type,
        "data": data,
    }


class CandidateEpisodeTest(unittest.TestCase):
    def test_explicit_episode_key_is_stable_and_ignores_human_labels(self):
        first = candidate(
            "record:plan-a",
            position=1,
            episode_id="episode:implementation-a",
            extra_data={"human_labels": {"root": True}},
        )
        second = candidate(
            "record:plan-b",
            position=2,
            episode_id="episode:implementation-a",
            extra_data={"human_labels": {"root": False}},
        )

        first_key = extract_episode_key(first, ())
        second_key = extract_episode_key(second, ())

        self.assertEqual(first_key, second_key)
        self.assertRegex(first_key or "", r"^episode:v1:[0-9a-f]{16}$")

    def test_path_materialization_anchor_groups_plan_and_execution(self):
        plan = candidate("record:plan", position=10)
        execution = candidate(
            "record:execution",
            position=20,
            decision_type="llm_tool_call",
            chosen_action="edit",
        )
        shared_change = path_node(
            "record:change",
            "change",
            phase="implementation",
        )
        seed = path_node("record:defect", "case.observed_defect")

        plan_key = extract_episode_key(
            plan,
            (
                plan["node"],
                path_node("record:action", "decision"),
                shared_change,
                seed,
            ),
        )
        execution_key = extract_episode_key(
            execution,
            (execution["node"], shared_change, seed),
        )

        self.assertEqual(plan_key, execution_key)
        self.assertIsNotNone(plan_key)

    def test_role_classification_uses_recorded_phase_and_event_semantics(self):
        cases = (
            (candidate("record:plan", position=1), "authored_plan"),
            (
                candidate(
                    "record:execution",
                    position=2,
                    decision_type="llm_tool_call",
                    chosen_action="edit",
                ),
                "execution",
            ),
            (
                candidate(
                    "record:verification",
                    position=3,
                    event_type="verification",
                ),
                "verification",
            ),
            (
                candidate(
                    "record:closure",
                    position=4,
                    phase="closure",
                ),
                "closure",
            ),
        )

        self.assertEqual(
            [classify_episode_role(value) for value, _ in cases],
            [expected for _, expected in cases],
        )

    def test_reasoning_block_remains_a_plan_with_stream_control_action(self):
        value = candidate(
            "record:plan",
            position=1,
            decision_type="reasoning_block",
            chosen_action="continue_processing_stream",
        )

        self.assertEqual(
            classify_episode_role(value),
            "authored_plan",
        )

    def test_plan_episode_precedes_earlier_execution_only_episode(self):
        values = (
            candidate(
                "record:early-execution",
                position=1,
                episode_id="episode:execution",
                decision_type="llm_tool_call",
                chosen_action="edit",
            ),
            candidate(
                "record:later-plan",
                position=10,
                episode_id="episode:plan",
            ),
        )

        result = select_episode_diverse_reserve(values, {}, limit=1)

        self.assertEqual(result.reserve_refs, ("record:later-plan",))

    def test_episode_keeps_earliest_plan_and_latest_lifecycle_member(self):
        values = (
            candidate(
                "record:late-plan",
                position=20,
                episode_id="episode:a",
            ),
            candidate(
                "record:execution",
                position=30,
                episode_id="episode:a",
                decision_type="llm_tool_call",
                chosen_action="edit",
            ),
            candidate(
                "record:early-plan",
                position=10,
                episode_id="episode:a",
            ),
            candidate(
                "record:verification",
                position=40,
                episode_id="episode:a",
                event_type="verification",
            ),
        )

        result = select_episode_diverse_reserve(values, {}, limit=8)
        audit = {item.ref: item for item in result.audit}

        self.assertEqual(
            result.reserve_refs,
            ("record:early-plan", "record:verification"),
        )
        self.assertEqual(audit["record:early-plan"].episode_role, "authored_plan")
        self.assertEqual(audit["record:early-plan"].reserve_rank, 0)
        self.assertEqual(
            audit["record:early-plan"].reason,
            "earliest_authored_plan_in_episode",
        )
        self.assertEqual(audit["record:verification"].reserve_rank, 1)
        self.assertEqual(
            audit["record:verification"].reason,
            "latest_verification_in_episode",
        )
        self.assertIsNone(audit["record:late-plan"].reserve_rank)
        self.assertEqual(
            audit["record:late-plan"].reason,
            "non_representative_episode_member",
        )
        self.assertIsNone(audit["record:execution"].reserve_rank)

    def test_multiple_episodes_are_round_robined_before_second_representatives(self):
        values = (
            candidate("record:a-plan", position=10, episode_id="episode:a"),
            candidate(
                "record:a-close",
                position=40,
                episode_id="episode:a",
                phase="closure",
            ),
            candidate("record:b-plan", position=20, episode_id="episode:b"),
            candidate(
                "record:b-verify",
                position=30,
                episode_id="episode:b",
                event_type="verification",
            ),
        )

        result = select_episode_diverse_reserve(values, {}, limit=4)

        self.assertEqual(
            result.reserve_refs,
            (
                "record:a-plan",
                "record:b-plan",
                "record:a-close",
                "record:b-verify",
            ),
        )
        self.assertEqual(
            [item.reserve_rank for item in result.selected_audit],
            [0, 1, 2, 3],
        )

    def test_unresolved_candidates_enter_fallback_after_episode_representatives(self):
        parsed_plan = candidate(
            "record:parsed-plan",
            position=10,
            episode_id="episode:parsed",
        )
        fallback_late = candidate(
            "record:fallback-late",
            position=30,
            extra_data={"human_labels": {"must_select": True}},
        )
        fallback_early = candidate(
            "record:fallback-early",
            position=20,
        )

        result = select_episode_diverse_reserve(
            (fallback_late, parsed_plan, fallback_early),
            {},
            limit=3,
        )
        audit = {item.ref: item for item in result.audit}

        self.assertEqual(
            result.reserve_refs,
            (
                "record:parsed-plan",
                "record:fallback-early",
                "record:fallback-late",
            ),
        )
        self.assertEqual(
            result.fallback_refs,
            ("record:fallback-early", "record:fallback-late"),
        )
        self.assertEqual(
            audit["record:fallback-early"].episode_key,
            "fallback",
        )
        self.assertEqual(
            audit["record:fallback-early"].reason,
            "episode_unresolved_fallback",
        )

    def test_reserve_limit_is_audited_without_changing_stable_full_order(self):
        values = (
            candidate("record:a-plan", position=10, episode_id="episode:a"),
            candidate(
                "record:a-close",
                position=40,
                episode_id="episode:a",
                phase="closure",
            ),
            candidate("record:b-plan", position=20, episode_id="episode:b"),
            candidate(
                "record:b-verify",
                position=30,
                episode_id="episode:b",
                event_type="verification",
            ),
        )

        limited = select_episode_diverse_reserve(
            tuple(reversed(values)),
            {},
            limit=2,
        )
        audit = {item.ref: item for item in limited.audit}

        self.assertEqual(
            limited.reserve_refs,
            ("record:a-plan", "record:b-plan"),
        )
        self.assertEqual(
            audit["record:a-close"].reason,
            "reserve_limit",
        )
        self.assertEqual(
            audit["record:b-verify"].reason,
            "reserve_limit",
        )

    def test_candidate_paths_can_be_supplied_separately_by_ref(self):
        value = candidate("record:plan", position=10)
        paths = {
            "record:plan": (
                value["node"],
                path_node("record:change", "change"),
                path_node("record:defect", "case.observed_defect"),
            )
        }

        result = select_episode_diverse_reserve((value,), paths, limit=1)

        self.assertEqual(result.reserve_refs, ("record:plan",))
        self.assertNotEqual(result.audit[0].episode_key, "fallback")

    def test_negative_or_boolean_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            select_episode_diverse_reserve((), {}, limit=-1)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            select_episode_diverse_reserve((), {}, limit=True)


if __name__ == "__main__":
    unittest.main()
