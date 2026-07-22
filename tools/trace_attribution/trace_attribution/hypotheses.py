"""Immutable hypothesis bookkeeping and resumable recursive traversal state."""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .causal_state import AttributionHypothesis, DefectState, FrontierItem, HypothesisEvidence
from .models import JsonDict


FRONTIER_CHECKPOINT_SCHEMA = "trace_attribution.recursive_frontier"
FRONTIER_CHECKPOINT_VERSION = 1


def _normalized_evidence_reason(reason: str) -> str:
    return " ".join(str(reason).split()).casefold()


def _evidence_identity(item: HypothesisEvidence) -> Tuple[str, str]:
    return (str(item.ref).strip(), _normalized_evidence_reason(item.reason))


def _canonical_evidence(item: HypothesisEvidence) -> HypothesisEvidence:
    return HypothesisEvidence(
        str(item.ref).strip(), " ".join(str(item.reason).split()), item.confidence
    )


def dedupe_evidence(items: Iterable[HypothesisEvidence]) -> Tuple[HypothesisEvidence, ...]:
    """Dedupe semantic evidence and retain the strongest finite confidence."""
    output: List[HypothesisEvidence] = []
    positions: Dict[Tuple[str, str], int] = {}
    for item in items:
        canonical = _canonical_evidence(item)
        key = _evidence_identity(canonical)
        position = positions.get(key)
        if position is None:
            positions[key] = len(output)
            output.append(canonical)
        elif canonical.confidence > output[position].confidence:
            existing = output[position]
            output[position] = HypothesisEvidence(
                existing.ref, existing.reason, canonical.confidence
            )
    return tuple(output)


def hypothesis_order_key(hypothesis: AttributionHypothesis) -> Tuple[float, int, str]:
    """Order investigation work without making a root-cause decision."""
    support_strength = sum(item.confidence for item in hypothesis.supporting_evidence)
    return (-support_strength, len(hypothesis.unresolved_questions), hypothesis.hypothesis_id)


def _dedupe_strings(items: Iterable[str]) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for item in items:
        value = str(item)
        if value not in seen:
            seen.add(value)
            output.append(value)
    return tuple(output)


@dataclass(frozen=True)
class _CompletedFrontierItem:
    item: FrontierItem
    evidence_hash: str


@dataclass(frozen=True)
class _FrontierMigration:
    queued: Tuple[FrontierItem, ...]
    in_flight: Tuple[FrontierItem, ...]
    completed: Tuple[_CompletedFrontierItem, ...]


class HypothesisLedger:
    """Maintains competing immutable hypotheses for one causal investigation."""

    def __init__(self) -> None:
        self._items: Dict[str, AttributionHypothesis] = {}

    def create(
        self,
        claim: str,
        candidate_root_ref: str,
        defect_state: DefectState,
        *,
        seed_binding_identity: str = "",
    ) -> AttributionHypothesis:
        hypothesis = AttributionHypothesis.create(
            claim,
            candidate_root_ref,
            defect_state,
            seed_binding_identity=seed_binding_identity,
        )
        existing = self._items.get(hypothesis.hypothesis_id)
        if existing is not None:
            return existing
        self._items[hypothesis.hypothesis_id] = hypothesis
        return hypothesis

    def get(self, hypothesis_id: str) -> AttributionHypothesis:
        return self._items[hypothesis_id]

    def add_support(
        self, hypothesis_id: str, ref: str, reason: str, confidence: float
    ) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        evidence = HypothesisEvidence(ref, reason, confidence)
        status = "supported" if item.status == "active" else item.status
        return self._replace_item(
            hypothesis_id,
            item.with_updates(
                supporting_evidence=dedupe_evidence([*item.supporting_evidence, evidence]),
                status=status,
            ),
        )

    def add_opposition(
        self, hypothesis_id: str, ref: str, reason: str, confidence: float
    ) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        evidence = HypothesisEvidence(ref, reason, confidence)
        return self._replace_item(
            hypothesis_id,
            item.with_updates(opposing_evidence=dedupe_evidence([*item.opposing_evidence, evidence])),
        )

    def add_unresolved_question(
        self,
        hypothesis_id: str,
        question: str,
        *,
        frontier: "RecursiveFrontier",
    ) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        changes = {
            "unresolved_questions": _dedupe_strings([*item.unresolved_questions, question])
        }
        return self._update_with_frontier(frontier, hypothesis_id, **changes)

    def _update_with_frontier(
        self,
        frontier: "RecursiveFrontier",
        hypothesis_id: str,
        **changes: Any,
    ) -> AttributionHypothesis:
        """Atomically rekey ledger and every frontier lifecycle state."""
        item = self.get(hypothesis_id)
        updated = item.with_updates(**changes)
        replacement_items = self._replacement_items(hypothesis_id, updated)
        migration = frontier._plan_hypothesis_migration(item, updated)
        previous_items = self._items
        try:
            frontier._apply_hypothesis_migration(migration)
            self._items = replacement_items
        except Exception:
            self._items = previous_items
            raise
        return updated

    def reject(self, hypothesis_id: str, reason: str) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        return self._replace_item(
            hypothesis_id, item.with_updates(status="rejected", resolution_reason=reason)
        )

    def reject_with_frontier(
        self,
        hypothesis_id: str,
        reason: str,
        *,
        opposing_refs: Iterable[str],
        frontier: "RecursiveFrontier",
        evidence_hash: str,
    ) -> AttributionHypothesis:
        """Atomically reject one hypothesis and terminate all of its live work."""
        item = self.get(hypothesis_id)
        if item.status not in {"active", "supported"}:
            raise ValueError("only an active hypothesis can be rejected")
        opposition = [
            HypothesisEvidence(str(ref), reason, 1.0) for ref in opposing_refs
        ]
        updated = item.with_updates(
            opposing_evidence=dedupe_evidence(
                [*item.opposing_evidence, *opposition]
            ),
            status="rejected",
            resolution_reason=reason,
        )
        replacement_items = self._replacement_items(hypothesis_id, updated)
        migration = frontier._plan_hypothesis_termination(
            hypothesis_id, evidence_hash=evidence_hash
        )
        frontier._apply_hypothesis_migration(migration)
        self._items = replacement_items
        return updated

    def supersede(
        self, hypothesis_id: str, successor_hypothesis_id: str, reason: str
    ) -> AttributionHypothesis:
        if successor_hypothesis_id not in self._items:
            raise KeyError(successor_hypothesis_id)
        item = self.get(hypothesis_id)
        return self._replace_item(
            hypothesis_id,
            item.with_updates(
                alternative_hypothesis_ids=_dedupe_strings(
                    [*item.alternative_hypothesis_ids, successor_hypothesis_id]
                ),
                status="superseded",
                resolution_reason=reason,
            ),
        )

    def best_active(self) -> Optional[AttributionHypothesis]:
        active = [
            item for item in self._items.values() if item.status in {"active", "supported"}
        ]
        return min(active, key=hypothesis_order_key) if active else None

    def snapshot(self) -> List[JsonDict]:
        return [self._items[key].to_dict() for key in sorted(self._items)]

    @classmethod
    def from_snapshot(cls, snapshot: Sequence[Mapping[str, object]]) -> "HypothesisLedger":
        ledger = cls()
        for value in snapshot:
            hypothesis = AttributionHypothesis.from_dict(dict(value))
            cls._validate_snapshot_evidence(hypothesis.supporting_evidence)
            cls._validate_snapshot_evidence(hypothesis.opposing_evidence)
            if hypothesis.hypothesis_id in ledger._items:
                raise ValueError("duplicate hypothesis in ledger snapshot")
            ledger._items[hypothesis.hypothesis_id] = hypothesis
        return ledger

    @staticmethod
    def _validate_snapshot_evidence(items: Tuple[HypothesisEvidence, ...]) -> None:
        if tuple(items) != dedupe_evidence(items):
            raise ValueError("hypothesis snapshot contains noncanonical evidence")

    def _replace_item(
        self, previous_hypothesis_id: str, updated: AttributionHypothesis
    ) -> AttributionHypothesis:
        if updated.semantic_hash != self.get(previous_hypothesis_id).semantic_hash:
            raise ValueError("semantic hypothesis updates require a frontier")
        self._items = self._replacement_items(previous_hypothesis_id, updated)
        return updated

    def _replacement_items(
        self, previous_hypothesis_id: str, updated: AttributionHypothesis
    ) -> Dict[str, AttributionHypothesis]:
        if previous_hypothesis_id not in self._items:
            raise KeyError(previous_hypothesis_id)
        if (
            updated.hypothesis_id != previous_hypothesis_id
            and updated.hypothesis_id in self._items
        ):
            raise ValueError("updated hypothesis duplicates an existing semantic identity")

        output = dict(self._items)
        output.pop(previous_hypothesis_id)
        output[updated.hypothesis_id] = updated
        if updated.hypothesis_id == previous_hypothesis_id:
            return output
        for hypothesis_id, item in list(output.items()):
            alternatives = _dedupe_strings(
                updated.hypothesis_id if item_id == previous_hypothesis_id else item_id
                for item_id in item.alternative_hypothesis_ids
            )
            if alternatives != item.alternative_hypothesis_ids:
                output[hypothesis_id] = item.with_updates(alternative_hypothesis_ids=alternatives)
        return output


class RecursiveFrontier:
    """Heap-backed recursive work queue with explicit lifecycle and resume support."""

    def __init__(self) -> None:
        self._heap: List[Tuple[Tuple[float, int, int, str], FrontierItem]] = []
        self._queued: Set[str] = set()
        self._in_flight: Dict[str, FrontierItem] = {}
        self._completed: Dict[str, _CompletedFrontierItem] = {}

    def push(self, item: FrontierItem) -> bool:
        if self._visit_exists(item.visit_key):
            return False
        heapq.heappush(self._heap, (item.heap_key, item))
        self._queued.add(item.visit_key)
        return True

    def pop(self) -> FrontierItem:
        _, item = heapq.heappop(self._heap)
        self._queued.remove(item.visit_key)
        self._in_flight[item.visit_key] = item
        return item

    def __bool__(self) -> bool:
        return bool(self._heap)

    def in_flight_items(self) -> List[FrontierItem]:
        return sorted(self._in_flight.values(), key=lambda item: item.heap_key)

    def mark_completed(self, item: FrontierItem, evidence_hash: str) -> None:
        in_flight = self._in_flight.get(item.visit_key)
        if in_flight != item:
            raise ValueError("mark_completed requires the exact in-flight item")
        self._in_flight.pop(item.visit_key)
        self._completed[item.visit_key] = _CompletedFrontierItem(item, str(evidence_hash))

    def complete_if_in_flight(self, item: FrontierItem, evidence_hash: str) -> bool:
        if self._in_flight.get(item.visit_key) != item:
            return False
        self.mark_completed(item, evidence_hash)
        return True

    def reopen(
        self,
        item: FrontierItem,
        *,
        evidence_hash: str,
        reason: str,
        root_verifier_rejected: bool = False,
    ) -> bool:
        completed = self._completed.get(item.visit_key)
        if completed is None or completed.item != item:
            return False
        if completed.evidence_hash == evidence_hash and not root_verifier_rejected:
            return False
        replacement = replace(item, reopen_reason=reason, evidence_hash=evidence_hash)
        if self._visit_exists(replacement.visit_key, excluding_completed=item.visit_key):
            return False

        heap = list(self._heap)
        queued = set(self._queued)
        completed_items = dict(self._completed)
        heapq.heappush(heap, (replacement.heap_key, replacement))
        queued.add(replacement.visit_key)
        completed_items.pop(item.visit_key)
        self._heap = heap
        self._queued = queued
        self._completed = completed_items
        return True

    def snapshot(self) -> List[JsonDict]:
        return [item.to_dict() for _, item in sorted(self._heap)]

    @classmethod
    def from_snapshot(cls, snapshot: Sequence[Mapping[str, object]]) -> "RecursiveFrontier":
        frontier = cls()
        for value in snapshot:
            item = FrontierItem.from_dict(dict(value))
            if not frontier.push(item):
                raise ValueError("duplicate frontier visit in snapshot")
        return frontier

    def checkpoint(self) -> JsonDict:
        return {
            "schema": FRONTIER_CHECKPOINT_SCHEMA,
            "version": FRONTIER_CHECKPOINT_VERSION,
            "queued": self.snapshot(),
            "in_flight": [item.to_dict() for item in self.in_flight_items()],
            "completed": [
                {"item": completed.item.to_dict(), "evidence_hash": completed.evidence_hash}
                for _, completed in sorted(self._completed.items())
            ],
        }

    @classmethod
    def from_checkpoint(cls, checkpoint: Mapping[str, object]) -> "RecursiveFrontier":
        """Restore completed work and requeue interrupted work by its stable heap key."""
        if not isinstance(checkpoint, Mapping):
            raise ValueError("frontier checkpoint must be a mapping")
        if checkpoint.get("schema") != FRONTIER_CHECKPOINT_SCHEMA:
            raise ValueError("unsupported frontier checkpoint schema")
        version = checkpoint.get("version")
        if type(version) is not int or version != FRONTIER_CHECKPOINT_VERSION:
            raise ValueError("unsupported frontier checkpoint version")
        for section in ("queued", "in_flight", "completed"):
            if section not in checkpoint:
                raise ValueError("frontier checkpoint missing {0}".format(section))
        queued = cls._load_checkpoint_items(checkpoint["queued"], "queued")
        in_flight = cls._load_checkpoint_items(checkpoint["in_flight"], "in_flight")
        completed = cls._load_completed_items(checkpoint["completed"])
        all_visits: Dict[str, str] = {}
        for state, items in (("queued", queued), ("in-flight", in_flight)):
            for item in items:
                cls._register_visit(all_visits, item.visit_key, state)
        for item in completed:
            cls._register_visit(all_visits, item.item.visit_key, "completed")

        frontier = cls()
        frontier._set_state(
            tuple([*queued, *in_flight]),
            tuple(),
            tuple(completed),
        )
        return frontier

    def _plan_hypothesis_migration(
        self, previous: AttributionHypothesis, updated: AttributionHypothesis
    ) -> _FrontierMigration:
        queued = tuple(
            self._migrate_item(item, previous, updated) for _, item in self._heap
        )
        in_flight = tuple(
            self._migrate_item(item, previous, updated) for item in self._in_flight.values()
        )
        completed = tuple(
            _CompletedFrontierItem(
                self._migrate_item(value.item, previous, updated), value.evidence_hash
            )
            for value in self._completed.values()
        )
        self._validate_lifecycle_state(queued, in_flight, completed)
        return _FrontierMigration(queued, in_flight, completed)

    def _plan_hypothesis_termination(
        self, hypothesis_id: str, *, evidence_hash: str
    ) -> _FrontierMigration:
        queued = tuple(
            item for _, item in self._heap if item.hypothesis_id != hypothesis_id
        )
        in_flight = tuple(
            item
            for item in self._in_flight.values()
            if item.hypothesis_id != hypothesis_id
        )
        terminated = [
            item for _, item in self._heap if item.hypothesis_id == hypothesis_id
        ] + [
            item
            for item in self._in_flight.values()
            if item.hypothesis_id == hypothesis_id
        ]
        completed = tuple(
            [*self._completed.values()]
            + [
                _CompletedFrontierItem(item, str(evidence_hash))
                for item in terminated
            ]
        )
        self._validate_lifecycle_state(queued, in_flight, completed)
        return _FrontierMigration(queued, in_flight, completed)

    def _apply_hypothesis_migration(self, migration: _FrontierMigration) -> None:
        self._validate_lifecycle_state(
            migration.queued, migration.in_flight, migration.completed
        )
        self._set_state(migration.queued, migration.in_flight, migration.completed)

    @staticmethod
    def _load_checkpoint_items(value: object, state: str) -> Tuple[FrontierItem, ...]:
        if not isinstance(value, list):
            raise ValueError("frontier checkpoint {0} must be a list".format(state))
        items: List[FrontierItem] = []
        for raw_item in value:
            if not isinstance(raw_item, Mapping):
                raise ValueError("frontier checkpoint {0} contains an invalid item".format(state))
            items.append(FrontierItem.from_dict(dict(raw_item)))
        return tuple(items)

    @staticmethod
    def _load_completed_items(value: object) -> Tuple[_CompletedFrontierItem, ...]:
        if not isinstance(value, list):
            raise ValueError("frontier checkpoint completed must be a list")
        items: List[_CompletedFrontierItem] = []
        for raw_item in value:
            if not isinstance(raw_item, Mapping) or not isinstance(raw_item.get("item"), Mapping):
                raise ValueError("frontier checkpoint completed contains an invalid item")
            items.append(
                _CompletedFrontierItem(
                    FrontierItem.from_dict(dict(raw_item["item"])),
                    str(raw_item.get("evidence_hash") or ""),
                )
            )
        return tuple(items)

    @staticmethod
    def _register_visit(visits: Dict[str, str], visit_key: str, state: str) -> None:
        existing = visits.get(visit_key)
        if existing is not None:
            raise ValueError(
                "frontier checkpoint repeats visit in {0} and {1}".format(existing, state)
            )
        visits[visit_key] = state

    def _set_state(
        self,
        queued: Sequence[FrontierItem],
        in_flight: Sequence[FrontierItem],
        completed: Sequence[_CompletedFrontierItem],
    ) -> None:
        heap: List[Tuple[Tuple[float, int, int, str], FrontierItem]] = []
        for item in queued:
            heapq.heappush(heap, (item.heap_key, item))
        self._heap = heap
        self._queued = {item.visit_key for item in queued}
        self._in_flight = {item.visit_key: item for item in in_flight}
        self._completed = {item.item.visit_key: item for item in completed}

    @staticmethod
    def _validate_lifecycle_state(
        queued: Sequence[FrontierItem],
        in_flight: Sequence[FrontierItem],
        completed: Sequence[_CompletedFrontierItem],
    ) -> None:
        visits: Dict[str, str] = {}
        for state, items in (("queued", queued), ("in-flight", in_flight)):
            for item in items:
                RecursiveFrontier._register_visit(visits, item.visit_key, state)
        for completed_item in completed:
            RecursiveFrontier._register_visit(visits, completed_item.item.visit_key, "completed")

    @staticmethod
    def _migrate_item(
        item: FrontierItem, previous: AttributionHypothesis, updated: AttributionHypothesis
    ) -> FrontierItem:
        if (
            item.hypothesis_id != previous.hypothesis_id
            or item.hypothesis_semantic_hash != previous.semantic_hash
        ):
            return item
        return FrontierItem.create(
            node_ref=item.node_ref,
            defect_state=item.defect_state,
            downstream_path=list(item.downstream_path),
            hypothesis_id=updated.hypothesis_id,
            hypothesis_semantic_hash=updated.semantic_hash,
            seed_binding_identity=item.seed_binding_identity,
            depth=item.depth,
            candidate_source=item.candidate_source,
            priority=item.priority,
            checked_evidence_refs=list(item.checked_evidence_refs),
            evidence_hash=item.evidence_hash,
            reopen_reason=item.reopen_reason,
            graph_position=item.graph_position,
        )

    def _visit_exists(self, visit_key: str, *, excluding_completed: str = "") -> bool:
        return (
            visit_key in self._queued
            or visit_key in self._in_flight
            or (visit_key in self._completed and visit_key != excluding_completed)
        )
