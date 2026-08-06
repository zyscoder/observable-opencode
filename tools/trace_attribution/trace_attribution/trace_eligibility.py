"""Pure alias and active-revision eligibility policy for semantic traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple


JsonDict = Dict[str, Any]
Node = Any
TraceRevision = Callable[[JsonDict], Tuple[Optional[str], str]]


@dataclass(frozen=True)
class RecordAliasIndex:
    aliases: Dict[str, str]
    owners: Dict[str, frozenset[str]]
    records_by_ref: Dict[str, JsonDict]


def build_record_alias_index(records: Iterable[Any]) -> RecordAliasIndex:
    aliases: Dict[str, str] = {}
    owner_sets: Dict[str, set[str]] = {}
    records_by_ref: Dict[str, JsonDict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            continue
        canonical = f"record:{record_id}"
        records_by_ref[canonical] = record
        for alias in record_aliases(record):
            prior = aliases.get(alias)
            if (
                prior is not None
                and prior != canonical
                and alias.startswith("failure_signature:")
            ):
                raise ValueError(
                    "failure_signature alias is ambiguous: {0}".format(alias)
                )
            aliases[alias] = canonical
            owner_sets.setdefault(alias, set()).add(canonical)
        aliases[canonical] = canonical
        owner_sets.setdefault(canonical, set()).add(canonical)
    return RecordAliasIndex(
        aliases=aliases,
        owners={alias: frozenset(owners) for alias, owners in owner_sets.items()},
        records_by_ref=records_by_ref,
    )


def record_aliases(record: Mapping[str, Any]) -> Iterable[str]:
    record_id = str(record.get("record_id") or "")
    event_type = str(record.get("event_type") or "")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if record_id:
        yield f"record:{record_id}"
        yield f"node:{record_id}"
    if event_type in ("evidence.semantic_fact", "evidence.fact") and record_id:
        yield f"evidence:{record_id}"
    if event_type == "external.evaluation_fact":
        if record_id:
            yield f"external_evaluation:{record_id}"
        if data.get("evaluation_id"):
            yield f"external_evaluation:{data['evaluation_id']}"
    if event_type == "case.observed_defect":
        if data.get("seed_id"):
            yield f"observed_defect_seed:{data['seed_id']}"
        signature = (
            data.get("failure_signature")
            if isinstance(data.get("failure_signature"), dict)
            else {}
        )
        if signature.get("signature_id"):
            yield f"failure_signature:{signature['signature_id']}"
    if event_type == "change":
        if record_id:
            yield f"change:{record_id}"
        if data.get("change_id"):
            yield f"change:{data['change_id']}"
    if event_type == "verification" and data.get("verification_id"):
        yield f"verification:{data['verification_id']}"
    if event_type == "response.output" and data.get("segment_id"):
        yield f"response_segment:{data['segment_id']}"
    if event_type == "response.claim":
        if record_id:
            yield f"response_claim:{record_id}"
        if data.get("claim_id"):
            yield f"response_claim:{data['claim_id']}"
    if event_type == "claim.support_assessment":
        if record_id:
            yield f"claim_support:{record_id}"
        if data.get("assessment_id"):
            yield f"claim_support:{data['assessment_id']}"
    if event_type == "decision" and data.get("decision_id"):
        yield f"decision:{data['decision_id']}"
    call_id = data.get("call_id") or data.get("callID")
    if call_id:
        if event_type == "tool.error":
            yield f"tool_error:{call_id}"
        elif event_type == "tool.result":
            yield f"tool_result:{call_id}"
        elif event_type == "tool.call":
            yield f"tool_call:{call_id}"
        elif event_type == "mcp.call":
            yield f"mcp:{call_id}"


def resolve_edge_endpoint(
    endpoint: Any,
    aliases: Mapping[str, str],
) -> Optional[str]:
    if not isinstance(endpoint, dict):
        return None
    ref_type = endpoint.get("type")
    ref_id = endpoint.get("id")
    if not ref_type or not ref_id:
        return None
    candidates = [f"{ref_type}:{ref_id}", f"record:{ref_id}", f"node:{ref_id}"]
    for candidate in candidates:
        resolved = resolve_ref(candidate, aliases)
        if resolved:
            return resolved
    return None


def resolve_ref(ref: str, aliases: Mapping[str, str]) -> Optional[str]:
    if ref in aliases:
        return aliases[ref]
    if ref.startswith("record:"):
        return aliases.get(ref)
    if ":" not in ref:
        return aliases.get(f"record:{ref}") or aliases.get(f"node:{ref}")
    return None


class TraceEligibilityPolicy:
    """Evaluate active-revision evidence without graph or I/O dependencies."""

    def __init__(
        self,
        *,
        trace: JsonDict,
        refs: Iterable[str],
        resolve: Callable[[str], Optional[str]],
        node_for_ref: Callable[[str], Optional[Node]],
        node_data: Callable[[Node], Mapping[str, Any]],
        node_event_type: Callable[[Node], str],
        evidence_eligible: Callable[[str, Node], bool],
        trace_execution_revision: TraceRevision,
    ) -> None:
        self.trace = trace
        self.refs = tuple(refs)
        self.resolve = resolve
        self.node_for_ref = node_for_ref
        self.node_data = node_data
        self.node_event_type = node_event_type
        self.evidence_eligible = evidence_eligible
        self.trace_execution_revision = trace_execution_revision
        self._active_repository_revision_ready = False
        self._active_repository_revision: Optional[int] = None

    def subject_provenance_binding_eligible(
        self,
        node: Node,
        *,
        require_formal_binding: bool = False,
    ) -> bool:
        data = self.node_data(node)
        if "revision_provenance_status" in data and (
            not isinstance(data.get("revision_provenance_status"), str)
            or data["revision_provenance_status"].strip().lower() != "valid"
        ):
            return False
        manifest = (
            self.trace.get("manifest")
            if isinstance(self.trace.get("manifest"), Mapping)
            else {}
        )
        manifest_declares_revision = manifest.get("subject_revision") not in (
            None,
            "",
        )
        active_subject_revision, provenance_status = self.trace_execution_revision(
            self.trace
        )
        revision_bearing = any(
            key in data
            for key in (
                "repository_revision",
                "revision_before",
                "revision_after",
                "subject_revision",
            )
        )
        binding_required = (
            require_formal_binding and manifest_declares_revision
        ) or (provenance_status == "valid" and revision_bearing)
        if binding_required:
            return bool(
                provenance_status == "valid"
                and isinstance(data.get("subject_revision"), str)
                and data["subject_revision"].strip()
                == str(active_subject_revision or "").strip()
                and isinstance(data.get("revision_provenance_status"), str)
                and data["revision_provenance_status"].strip().lower() == "valid"
            )
        subject_revision = data.get("subject_revision")
        if subject_revision in (None, ""):
            return True
        if not isinstance(subject_revision, str):
            return False
        manifest_revision = manifest.get("subject_revision")
        return not (
            isinstance(manifest_revision, str)
            and manifest_revision.strip()
            and subject_revision.strip() != manifest_revision.strip()
        )

    def authority_value(self, node: Node) -> Any:
        data = self.node_data(node)
        event_type = self.node_event_type(node)
        if event_type == "response.claim":
            return data.get("repository_revision")
        if (
            event_type == "verification"
            and data.get("effective_for_final_state") is True
        ):
            return data.get("repository_revision")
        if event_type == "change":
            return data.get("revision_after")
        return None

    def eligible_repository_revision_authority(
        self,
        ref: str,
        node: Node,
    ) -> Optional[int]:
        if not self.evidence_eligible(ref, node):
            return None
        if not self.subject_provenance_binding_eligible(
            node,
            require_formal_binding=True,
        ):
            return None
        data = self.node_data(node)
        if (
            data.get("audit_only") is True
            or data.get("offline_only") is True
            or data.get("eligible_for_attribution") is False
            or str(data.get("behavior_impact") or "").strip().lower()
            in {"none", "none_offline_analysis_only"}
        ):
            return None
        revision_status = data.get("revision_status")
        if revision_status not in (None, "") and (
            not isinstance(revision_status, str)
            or revision_status.strip().lower() != "matched"
        ):
            return None
        value = self.authority_value(node)
        return value if type(value) is int and value >= 0 else None

    def active_repository_revision(self) -> Optional[int]:
        if self._active_repository_revision_ready:
            return self._active_repository_revision
        revisions = [
            value
            for ref in self.refs
            for node in [self.node_for_ref(ref)]
            if node is not None
            for value in [self.eligible_repository_revision_authority(ref, node)]
            if value is not None
        ]
        self._active_repository_revision = max(revisions) if revisions else None
        self._active_repository_revision_ready = True
        return self._active_repository_revision

    def active_revision_evidence_eligible(
        self,
        ref: str,
        *,
        require_formal_binding: bool = False,
    ) -> bool:
        return self._active_revision_evidence_eligible(
            str(ref),
            require_formal_binding=require_formal_binding,
            visiting=frozenset(),
        )

    def _active_revision_evidence_eligible(
        self,
        ref: str,
        *,
        require_formal_binding: bool,
        visiting: frozenset[str],
    ) -> bool:
        resolved = self.resolve(ref)
        if not resolved or resolved in visiting:
            return False
        node = self.node_for_ref(resolved)
        if node is None or not self.evidence_eligible(resolved, node):
            return False
        if not self.subject_provenance_binding_eligible(
            node,
            require_formal_binding=require_formal_binding,
        ):
            return False
        data = self.node_data(node)
        value = self.authority_value(node)
        if (
            value is not None
            and self.eligible_repository_revision_authority(resolved, node) is None
        ):
            return False
        if self.node_event_type(node) == "progress.episode":
            member_refs = [
                self.resolve(str(item)) or str(item)
                for item in data.get("member_refs") or ()
                if str(item)
            ]
            next_visiting = visiting | {resolved}
            if member_refs and not any(
                member_ref != resolved
                and self.node_for_ref(member_ref) is not None
                and self._active_revision_evidence_eligible(
                    member_ref,
                    require_formal_binding=False,
                    visiting=next_visiting,
                )
                for member_ref in member_refs
            ):
                return False
        revision_status = data.get("revision_status")
        if revision_status not in (None, "") and (
            not isinstance(revision_status, str)
            or revision_status.strip().lower() != "matched"
        ):
            return False

        required_revision_field = ""
        if self.node_event_type(node) == "response.claim":
            required_revision_field = "repository_revision"
        elif (
            self.node_event_type(node) == "verification"
            and data.get("effective_for_final_state") is True
        ):
            required_revision_field = "repository_revision"
        elif self.node_event_type(node) == "change":
            required_revision_field = "revision_after"
        revision_field = required_revision_field
        if not revision_field and data.get("repository_revision") is not None:
            revision_field = "repository_revision"
        revision_value = data.get(revision_field) if revision_field else None
        if revision_value is not None:
            if type(revision_value) is not int or revision_value < 0:
                return False
            if (
                self.active_repository_revision() is not None
                and revision_value != self.active_repository_revision()
            ):
                return False
        elif required_revision_field and self.active_repository_revision() is not None:
            return False
        return True
