"""Immutable hypothesis bookkeeping and resumable recursive traversal state."""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .causal_state import AttributionHypothesis, DefectState, FrontierItem, HypothesisEvidence
from .models import JsonDict


def dedupe_evidence(items: Iterable[HypothesisEvidence]) -> Tuple[HypothesisEvidence, ...]:
    """Keep first-seen immutable evidence records in their auditable order."""
    output: List[HypothesisEvidence] = []
    seen = set()
    for item in items:
        key = (item.ref, item.reason, item.confidence)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
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
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return tuple(output)


class HypothesisLedger:
    """Maintains competing immutable hypotheses for one causal investigation."""

    def __init__(self) -> None:
        self._items: Dict[str, AttributionHypothesis] = {}

    def create(
        self, claim: str, candidate_root_ref: str, defect_state: DefectState
    ) -> AttributionHypothesis:
        hypothesis = AttributionHypothesis.create(claim, candidate_root_ref, defect_state)
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

    def add_unresolved_question(self, hypothesis_id: str, question: str) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        return self._replace_item(
            hypothesis_id,
            item.with_updates(
                unresolved_questions=_dedupe_strings([*item.unresolved_questions, question])
            ),
        )

    def reject(self, hypothesis_id: str, reason: str) -> AttributionHypothesis:
        item = self.get(hypothesis_id)
        return self._replace_item(
            hypothesis_id, item.with_updates(status="rejected", resolution_reason=reason)
        )

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
            if hypothesis.hypothesis_id in ledger._items:
                raise ValueError("duplicate hypothesis in ledger snapshot")
            ledger._items[hypothesis.hypothesis_id] = hypothesis
        return ledger

    def _replace_item(
        self, previous_hypothesis_id: str, updated: AttributionHypothesis
    ) -> AttributionHypothesis:
        if previous_hypothesis_id not in self._items:
            raise KeyError(previous_hypothesis_id)
        if (
            updated.hypothesis_id != previous_hypothesis_id
            and updated.hypothesis_id in self._items
        ):
            raise ValueError("updated hypothesis duplicates an existing semantic identity")

        self._items.pop(previous_hypothesis_id)
        self._items[updated.hypothesis_id] = updated
        if updated.hypothesis_id != previous_hypothesis_id:
            self._replace_alternative_references(previous_hypothesis_id, updated.hypothesis_id)
        return updated

    def _replace_alternative_references(self, previous_hypothesis_id: str, updated_hypothesis_id: str) -> None:
        for hypothesis_id, item in list(self._items.items()):
            alternatives = tuple(
                updated_hypothesis_id if item_id == previous_hypothesis_id else item_id
                for item_id in item.alternative_hypothesis_ids
            )
            alternatives = _dedupe_strings(alternatives)
            if alternatives == item.alternative_hypothesis_ids:
                continue
            self._items[hypothesis_id] = item.with_updates(
                alternative_hypothesis_ids=alternatives
            )


class RecursiveFrontier:
    """Heap-backed recursive work queue with semantic merge and resume support."""

    def __init__(self) -> None:
        self._heap: List[Tuple[Tuple[float, int, int, str], FrontierItem]] = []
        self._queued: Set[str] = set()
        self._completed_evidence: Dict[str, str] = {}

    def push(self, item: FrontierItem) -> bool:
        if item.visit_key in self._queued or item.visit_key in self._completed_evidence:
            return False
        heapq.heappush(self._heap, (item.heap_key, item))
        self._queued.add(item.visit_key)
        return True

    def pop(self) -> FrontierItem:
        _, item = heapq.heappop(self._heap)
        self._queued.remove(item.visit_key)
        return item

    def __bool__(self) -> bool:
        return bool(self._heap)

    def mark_completed(self, item: FrontierItem, evidence_hash: str) -> None:
        self._completed_evidence[item.visit_key] = evidence_hash

    def reopen(
        self,
        item: FrontierItem,
        *,
        evidence_hash: str,
        reason: str,
        root_verifier_rejected: bool = False,
    ) -> bool:
        completed_evidence_hash = self._completed_evidence.get(item.visit_key)
        if completed_evidence_hash is None:
            return False
        if completed_evidence_hash == evidence_hash and not root_verifier_rejected:
            return False
        self._completed_evidence.pop(item.visit_key)
        return self.push(
            replace(item, reopen_reason=reason, evidence_hash=evidence_hash)
        )

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
            "queued": self.snapshot(),
            "completed_evidence": [
                {"visit_key": visit_key, "evidence_hash": evidence_hash}
                for visit_key, evidence_hash in sorted(self._completed_evidence.items())
            ],
        }

    @classmethod
    def from_checkpoint(cls, checkpoint: Mapping[str, object]) -> "RecursiveFrontier":
        queued = checkpoint.get("queued")
        frontier = cls.from_snapshot(queued if isinstance(queued, list) else [])
        completed_evidence = checkpoint.get("completed_evidence")
        if not isinstance(completed_evidence, list):
            return frontier
        for value in completed_evidence:
            if not isinstance(value, Mapping):
                continue
            visit_key = str(value.get("visit_key") or "")
            evidence_hash = str(value.get("evidence_hash") or "")
            if not visit_key or visit_key in frontier._completed_evidence:
                raise ValueError("invalid completed evidence in frontier checkpoint")
            if visit_key in frontier._queued:
                raise ValueError("frontier checkpoint repeats a queued visit as completed")
            frontier._completed_evidence[visit_key] = evidence_hash
        return frontier
