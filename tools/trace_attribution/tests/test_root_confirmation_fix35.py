from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from trace_attribution.checkpoint import CheckpointState
from trace_attribution.recursive_analyzer import (
    RecursiveAnalysisState,
    _validate_provider_state,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    artifact_state,
)
from tools.trace_attribution.tests.test_root_confirmation_fix34 import (
    _replay_fixture,
    _resign_provider_state,
    _state_snapshot,
)


def _full_cache_stats(*, enabled, path):
    return {
        "enabled": enabled,
        "path": path,
        "loaded_entries": 2,
        "hits": 3,
        "misses": 4,
        "writes": 5,
        "invalid_entries": 1,
        "corrupt_entries": 0,
        "write_error_count": 1,
        "write_errors": ["OSError: persisted cache failure"],
    }


def _provider_for_state(state, cache_stats, *, cache_identity="cache:test"):
    provider = {
        "schema": "recursive-provider-state/v1",
        "circuit": {
            "open": True,
            "reason": "persisted Fix35 circuit",
            "consecutive_provider_errors": 2,
            "provider_error_threshold": 7,
        },
        "cache_identity": cache_identity,
        "cache_stats": copy.deepcopy(cache_stats),
        "accounting": {
            "judge_requests": state.judge_requests,
            "judge_request_uncertainty_count": (
                state.judge_request_uncertainty_count
            ),
            "logical_judge_calls": state.logical_judge_calls,
            "logical_confirmation_calls": state.logical_confirmation_calls,
            "investigation_rounds": state.investigation_rounds,
            "artifact_bytes": state.artifact_bytes,
        },
    }
    _resign_provider_state(provider)
    return provider


def _checkpoint_fixture(root, cache_stats):
    graph, state, item, queued = artifact_state(root)
    state.seed_ledger[item.seed_binding_identity].candidate_refs.add(
        item.node_ref
    )
    state.processed_items = 1
    if not state.enqueue_confirmation(queued):
        raise AssertionError("failed to enqueue Fix35 checkpoint fixture")
    state.judge_requests = 5
    state.judge_request_uncertainty_count = 2
    state.logical_judge_calls = 7
    state.logical_confirmation_calls = 4
    state.investigation_rounds = 3
    state.artifact_bytes = 11
    provider = _provider_for_state(state, cache_stats)
    state.provider_state = copy.deepcopy(provider)

    def record(payload, *, operation=None):
        value = {
            "transaction_sequence": 1,
            "semantic_key": "fix35:snapshot",
            "payload": payload,
        }
        if operation is not None:
            value["operation"] = operation
        return value

    checkpoint = CheckpointState(
        config={"cache_identity": "cache:test"},
        run_id="fix35",
        transaction_sequence=1,
        frontier_records=(record(state.frontier_checkpoint_payload()),),
        hypothesis_records=(record(state.hypothesis_checkpoint_payload()),),
        actions=(
            record(
                state.action_checkpoint_payload(),
                operation="state_snapshot",
            ),
        ),
    )
    return graph, state, provider, checkpoint


class ProviderAccountingProjectionTest(unittest.TestCase):
    def test_resigned_drift_in_each_accounting_field_is_rejected_atomically(self):
        fields = (
            "judge_requests",
            "judge_request_uncertainty_count",
            "logical_judge_calls",
            "logical_confirmation_calls",
            "investigation_rounds",
            "artifact_bytes",
        )
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                action, state, judge, analyzer = _replay_fixture(
                    Path(directory)
                )
                provider = action["payload"]["provider_state"]
                provider["accounting"][field] += 1
                _resign_provider_state(provider)
                before = _state_snapshot(state, judge)

                with self.assertRaisesRegex(ValueError, "provider accounting"):
                    analyzer._confirm_queued_roots(state)

                self.assertEqual(_state_snapshot(state, judge), before)
                self.assertEqual(judge.confirmation_calls, 0)


class ProviderCacheStatsValidationTest(unittest.TestCase):
    def test_canonical_minimal_and_full_cache_stats_are_accepted_and_copied(self):
        forms = {
            "minimal_disabled": {"enabled": False},
            "full_enabled": _full_cache_stats(
                enabled=True,
                path="/tmp/fix35-enabled-cache.jsonl",
            ),
            "full_disabled": _full_cache_stats(enabled=False, path=""),
        }
        with tempfile.TemporaryDirectory() as directory:
            _, state, _, _ = artifact_state(Path(directory))
            for label, cache_stats in forms.items():
                with self.subTest(label=label):
                    provider = _provider_for_state(state, cache_stats)
                    canonical = _validate_provider_state(
                        provider,
                        state,
                        cache_identity="cache:test",
                    )

                    self.assertEqual(canonical, provider)
                    self.assertIsNot(
                        canonical["cache_stats"],
                        provider["cache_stats"],
                    )
                    provider["cache_stats"]["enabled"] = not cache_stats[
                        "enabled"
                    ]
                    self.assertEqual(
                        canonical["cache_stats"]["enabled"],
                        cache_stats["enabled"],
                    )

    def test_malformed_full_cache_stats_are_rejected_after_resigning(self):
        valid = _full_cache_stats(
            enabled=True,
            path="/tmp/fix35-enabled-cache.jsonl",
        )
        mutations = {
            "missing_key": lambda value: value.pop("hits"),
            "extra_key": lambda value: value.__setitem__("extra", 0),
            "wrong_bool": lambda value: value.__setitem__("enabled", 1),
            "wrong_string": lambda value: value.__setitem__("path", 7),
            "wrong_int": lambda value: value.__setitem__("hits", True),
            "negative_count": lambda value: value.__setitem__("misses", -1),
            "wrong_list": lambda value: value.__setitem__(
                "write_errors", ("failure",)
            ),
            "wrong_list_item": lambda value: value.__setitem__(
                "write_errors", ["failure", 1]
            ),
            "inconsistent_error_count": lambda value: value.__setitem__(
                "write_error_count", 0
            ),
            "enabled_empty_path": lambda value: value.__setitem__("path", ""),
            "disabled_nonempty_path": lambda value: value.__setitem__(
                "enabled", False
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            _, state, _, _ = artifact_state(Path(directory))
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    cache_stats = copy.deepcopy(valid)
                    mutate(cache_stats)
                    provider = _provider_for_state(state, cache_stats)

                    with self.assertRaisesRegex(ValueError, "cache"):
                        _validate_provider_state(
                            provider,
                            state,
                            cache_identity="cache:test",
                        )

    def test_noncanonical_minimal_enabled_cache_stats_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, state, _, _ = artifact_state(Path(directory))
            provider = _provider_for_state(
                state,
                {"enabled": True},
            )

            with self.assertRaisesRegex(ValueError, "cache"):
                _validate_provider_state(
                    provider,
                    state,
                    cache_identity="cache:test",
                )


class ProviderCacheStatsRestoreReplayTest(unittest.TestCase):
    def test_malformed_cache_stats_are_rejected_by_restore_and_replay(self):
        malformed = _full_cache_stats(
            enabled=True,
            path="/tmp/fix35-enabled-cache.jsonl",
        )
        malformed["hits"] = True
        with tempfile.TemporaryDirectory() as directory:
            graph, _, _, checkpoint = _checkpoint_fixture(
                Path(directory),
                malformed,
            )
            with self.assertRaisesRegex(ValueError, "cache"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=graph,
                    checkpoint=checkpoint,
                )

        with tempfile.TemporaryDirectory() as directory:
            action, state, judge, analyzer = _replay_fixture(
                Path(directory)
            )
            provider = action["payload"]["provider_state"]
            provider["cache_stats"] = copy.deepcopy(malformed)
            _resign_provider_state(provider)
            before = _state_snapshot(state, judge)

            with self.assertRaisesRegex(ValueError, "cache"):
                analyzer._confirm_queued_roots(state)

            self.assertEqual(_state_snapshot(state, judge), before)
            self.assertEqual(judge.confirmation_calls, 0)

    def test_valid_nonzero_restore_and_completed_replay_remain_supported(self):
        full_enabled = _full_cache_stats(
            enabled=True,
            path="/tmp/fix35-enabled-cache.jsonl",
        )
        with tempfile.TemporaryDirectory() as directory:
            graph, _, provider, checkpoint = _checkpoint_fixture(
                Path(directory),
                full_enabled,
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=checkpoint,
            )

        self.assertEqual(restored.provider_state, provider)
        self.assertEqual(
            (
                restored.judge_requests,
                restored.judge_request_uncertainty_count,
                restored.logical_judge_calls,
                restored.logical_confirmation_calls,
                restored.investigation_rounds,
                restored.artifact_bytes,
            ),
            (5, 2, 7, 4, 3, 11),
        )

        with tempfile.TemporaryDirectory() as directory:
            action, state, judge, analyzer = _replay_fixture(
                Path(directory)
            )
            replay_provider = action["payload"]["provider_state"]
            replay_provider["cache_stats"] = copy.deepcopy(full_enabled)
            _resign_provider_state(replay_provider)

            analyzer._confirm_queued_roots(state)

        self.assertEqual(state.provider_state, replay_provider)
        self.assertEqual(judge.confirmation_calls, 0)


if __name__ == "__main__":
    unittest.main()
