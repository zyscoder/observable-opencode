"""Deterministic seed-owned projection for v5 attribution reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .causal_state import seed_binding_identity_for
from .label_contract import V5_LABEL_SCHEMA_VERSION, validate_labels
from .models import stable_json


class SeedProjectionError(ValueError):
    """Raised when report role ownership cannot be projected unambiguously."""


@dataclass(frozen=True, order=True)
class ProjectedRoleOccurrence:
    seed_binding_identity: str
    semantic_occurrence_id: str
    role: str
    node_ref: str
    publication_sha256: str
    source_section: str

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (
            self.seed_binding_identity,
            self.semantic_occurrence_id,
            self.role,
            self.node_ref,
        )


@dataclass(frozen=True)
class SeedProjection:
    seed_binding_identity: str
    records: Tuple[ProjectedRoleOccurrence, ...]


@dataclass(frozen=True)
class SeedOwnedReportProjection:
    records: Tuple[ProjectedRoleOccurrence, ...]
    seeds: Tuple[SeedProjection, ...]

    def for_seed(self, seed_binding_identity: str) -> SeedProjection:
        matches = [
            seed
            for seed in self.seeds
            if seed.seed_binding_identity == seed_binding_identity
        ]
        if len(matches) != 1:
            raise SeedProjectionError("projection has no unique requested seed")
        return matches[0]


COMPARISON_V8_SCHEMA_VERSION = "recursive-attribution-comparison/v8"


@dataclass(frozen=True)
class ClassificationMetrics:
    tp: int
    fp: int
    fn: int
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass(frozen=True)
class MacroMetric:
    value: Optional[float]
    contributor_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "contributor_count": self.contributor_count,
        }


@dataclass(frozen=True)
class CoverageMetric:
    covered: int
    declared: int
    value: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "covered": self.covered,
            "declared": self.declared,
            "value": self.value,
        }


@dataclass(frozen=True)
class SeedMetricResult:
    seed_binding_identity: str
    defect_id: str
    expected_outcome: str
    report_outcome: str
    terminal_outcome: str
    matched_unresolved_outcome: Optional[str]
    unresolved_reasons: Tuple[str, ...]
    root_metrics: ClassificationMetrics
    top1_match: Optional[bool]
    role_pair_metrics: ClassificationMetrics
    outcome_agreement: bool
    terminal_valid: bool
    forbidden_root_passed: bool
    forbidden_root_occurrences: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_binding_identity": self.seed_binding_identity,
            "defect_id": self.defect_id,
            "expected_outcome": self.expected_outcome,
            "report_outcome": self.report_outcome,
            "terminal_outcome": self.terminal_outcome,
            "matched_unresolved_outcome": self.matched_unresolved_outcome,
            "unresolved_reasons": list(self.unresolved_reasons),
            "root_metrics": self.root_metrics.to_dict(),
            "top1_match": self.top1_match,
            "role_pair_metrics": self.role_pair_metrics.to_dict(),
            "outcome_agreement": self.outcome_agreement,
            "terminal_valid": self.terminal_valid,
            "forbidden_root_passed": self.forbidden_root_passed,
            "forbidden_root_occurrences": list(
                self.forbidden_root_occurrences
            ),
        }


@dataclass(frozen=True)
class SeedOwnedMetrics:
    case_id: str
    per_seed: Tuple[SeedMetricResult, ...]
    micro_root_metrics: ClassificationMetrics
    micro_role_pair_metrics: ClassificationMetrics
    macro_metrics: Tuple[Tuple[str, MacroMetric], ...]
    label_seed_coverage: CoverageMetric
    report_seed_coverage: CoverageMetric
    terminal_seed_coverage: CoverageMetric
    binding_safety_passed: bool
    checkpoint_safety_passed: bool
    global_terminal_state_valid: bool
    all_seed_coverage_passed: bool
    schema_version: str = COMPARISON_V8_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "per_seed": [item.to_dict() for item in self.per_seed],
            "micro": {
                "root_metrics": self.micro_root_metrics.to_dict(),
                "role_pair_metrics": self.micro_role_pair_metrics.to_dict(),
            },
            "macro": {
                name: metric.to_dict() for name, metric in self.macro_metrics
            },
            "coverage": {
                "label_seed_coverage": self.label_seed_coverage.to_dict(),
                "report_seed_coverage": self.report_seed_coverage.to_dict(),
                "terminal_seed_coverage": self.terminal_seed_coverage.to_dict(),
            },
            "safety": {
                "binding_safety_passed": self.binding_safety_passed,
                "checkpoint_safety_passed": self.checkpoint_safety_passed,
                "global_terminal_state_valid": self.global_terminal_state_valid,
            },
            "all_seed_coverage_passed": self.all_seed_coverage_passed,
        }


_ROLE_SECTIONS = (
    ("confirmed_roots", "node_ref", "confirmation", "root"),
    ("co_roots", "node_ref", "confirmation", "root"),
    (
        "contributing_conditions",
        "node_ref",
        "confirmation",
        "contributing_condition",
    ),
    (
        "amplifying_factors",
        "node_ref",
        "confirmation",
        "amplifying_factor",
    ),
    (
        "downstream_materializations",
        "candidate_ref",
        "role_judgment",
        "downstream_materialization",
    ),
    ("rejected_candidates", "node_ref", "confirmation", "unrelated"),
)
_NESTED_ROLE = {
    "root": "necessary_cause",
    "contributing_condition": "contributing_condition",
    "amplifying_factor": "amplifying_factor",
    "downstream_materialization": "downstream_materialization",
    "unrelated": "unrelated",
}


def _items(value: Any, label: str) -> Sequence[Any]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SeedProjectionError("{0} must be a list".format(label))
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SeedProjectionError("{0} must be an object".format(label))
    return value


def _required_string(value: Mapping[str, Any], field: str, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise SeedProjectionError(
            "{0}.{1} must be a non-empty string".format(label, field)
        )
    return item


def _occurrence(value: Mapping[str, Any], label: str) -> str:
    occurrence = _required_string(value, "semantic_occurrence_id", label)
    if not occurrence.startswith("semantic_occurrence:v1:"):
        raise SeedProjectionError(
            "{0}.semantic_occurrence_id is not versioned".format(label)
        )
    return occurrence


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:{0}".format(
        hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()
    )


def _asserted_bindings(
    value: Mapping[str, Any],
    label: str,
) -> Tuple[str, ...]:
    bindings = []

    def visit(candidate: Mapping[str, Any], path: str) -> None:
        if "seed_binding_identity" in candidate:
            scalar = candidate["seed_binding_identity"]
            if not isinstance(scalar, str) or not scalar:
                raise SeedProjectionError(
                    "{0}.seed_binding_identity has an invalid ownership type".format(
                        path
                    )
                )
            bindings.append(scalar)
        if "seed_binding_identities" in candidate:
            plural = candidate["seed_binding_identities"]
            if not isinstance(plural, (list, tuple)):
                raise SeedProjectionError(
                    "{0}.seed_binding_identities has an invalid ownership type".format(
                        path
                    )
                )
            for index, scalar in enumerate(plural):
                if not isinstance(scalar, str) or not scalar:
                    raise SeedProjectionError(
                        "{0}.seed_binding_identities[{1}] has an invalid ownership type".format(
                            path, index
                        )
                    )
                bindings.append(scalar)
        for field in ("owner", "provenance"):
            if field not in candidate:
                continue
            nested = candidate[field]
            if not isinstance(nested, Mapping):
                raise SeedProjectionError(
                    "{0}.{1} has an invalid ownership container".format(path, field)
                )
            visit(nested, "{0}.{1}".format(path, field))

    visit(value, label)
    return tuple(bindings)


def _owned_binding(
    publication: Mapping[str, Any],
    authoritative: Mapping[str, Any],
    known_seed_bindings: Iterable[str],
    label: str,
) -> str:
    owner = authoritative.get("seed_binding_identity")
    if not isinstance(owner, str) or not owner:
        raise SeedProjectionError("{0} has no authoritative owner".format(label))
    assertions = {
        *_asserted_bindings(publication, label),
        *_asserted_bindings(authoritative, "{0}.authoritative".format(label)),
    }
    if assertions != {owner}:
        raise SeedProjectionError(
            "{0} has conflicting or multiple ownership assertions".format(label)
        )
    if owner not in set(known_seed_bindings):
        raise SeedProjectionError("{0} has an unknown report seed owner".format(label))
    return owner


def _validated_label_seeds(labels_value: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    try:
        labels = validate_labels(labels_value)
    except (TypeError, ValueError) as error:
        raise SeedProjectionError("invalid labels: {0}".format(error)) from error
    if labels.get("schema_version") != V5_LABEL_SCHEMA_VERSION:
        raise SeedProjectionError("seed-owned projection requires v5 labels")
    seeds: Dict[str, Mapping[str, Any]] = {}
    for seed in labels["seeds"]:
        identity = str(seed["seed_binding_identity"])
        if identity in seeds:
            raise SeedProjectionError("labels contain a duplicate seed binding")
        seeds[identity] = seed
    return seeds


def _validate_report_seed_coverage(
    report: Mapping[str, Any],
    label_seeds: Mapping[str, Mapping[str, Any]],
) -> None:
    report_seeds = _items(report.get("seed_results"), "report.seed_results")
    seen = set()
    for index, raw_seed in enumerate(report_seeds):
        label = "report.seed_results[{0}]".format(index)
        seed = _mapping(raw_seed, label)
        start_ref = _required_string(seed, "start_ref", label)
        fingerprint = _required_string(seed, "defect_fingerprint", label)
        supplied = _required_string(seed, "seed_binding_identity", label)
        computed = seed_binding_identity_for(start_ref, fingerprint)
        if supplied != computed:
            raise SeedProjectionError(
                "{0} report seed identity does not match its inputs".format(label)
            )
        expected = label_seeds.get(supplied)
        if expected is None:
            raise SeedProjectionError("report seed coverage contains an extra seed")
        if (
            expected["start_ref"] != start_ref
            or expected["defect_fingerprint"] != fingerprint
        ):
            raise SeedProjectionError("report seed contradicts its label binding")
        if supplied in seen:
            raise SeedProjectionError("report seed coverage contains a duplicate seed")
        seen.add(supplied)
    if seen != set(label_seeds):
        raise SeedProjectionError("report seed coverage is missing a label seed")


def _validate_nested_role(
    judgment: Mapping[str, Any],
    *,
    node_ref: str,
    occurrence: str,
    role: str,
    label: str,
) -> None:
    if _required_string(judgment, "candidate_ref", label) != node_ref:
        raise SeedProjectionError("{0} candidate ref is contradictory".format(label))
    nested_occurrence = judgment.get("semantic_occurrence_id")
    if nested_occurrence not in (None, "", occurrence):
        raise SeedProjectionError(
            "{0} semantic occurrence is contradictory".format(label)
        )
    if role == "root" and judgment.get("status") != "confirmed":
        raise SeedProjectionError("{0} root confirmation is not confirmed".format(label))
    if judgment.get("factor_role") != _NESTED_ROLE[role]:
        raise SeedProjectionError("{0} factor role is contradictory".format(label))


def _add_record(
    records: Dict[Tuple[str, str, str, str], ProjectedRoleOccurrence],
    *,
    owner: str,
    occurrence: str,
    role: str,
    node_ref: str,
    source_section: str,
) -> None:
    key = (owner, occurrence, role, node_ref)
    if key in records:
        return
    digest = _digest(
        {
            "seed_binding_identity": owner,
            "semantic_occurrence_id": occurrence,
            "role": role,
            "node_ref": node_ref,
        }
    )
    records[key] = ProjectedRoleOccurrence(
        seed_binding_identity=owner,
        semantic_occurrence_id=occurrence,
        role=role,
        node_ref=node_ref,
        publication_sha256=digest,
        source_section=source_section,
    )


def _project_role_sections(
    report: Mapping[str, Any],
    known_seed_bindings: Iterable[str],
    records: Dict[Tuple[str, str, str, str], ProjectedRoleOccurrence],
) -> None:
    for section, ref_field, nested_field, role in _ROLE_SECTIONS:
        for index, raw_publication in enumerate(
            _items(report.get(section), "report.{0}".format(section))
        ):
            label = "report.{0}[{1}]".format(section, index)
            publication = _mapping(raw_publication, label)
            node_ref = _required_string(publication, ref_field, label)
            occurrence = _occurrence(publication, label)
            nested = _mapping(
                publication.get(nested_field),
                "{0}.{1}".format(label, nested_field),
            )
            owner = _owned_binding(
                publication,
                nested,
                known_seed_bindings,
                label,
            )
            if section == "rejected_candidates" and nested.get(
                "factor_role"
            ) == "unknown":
                if _required_string(
                    nested,
                    "candidate_ref",
                    "{0}.{1}".format(label, nested_field),
                ) != node_ref:
                    raise SeedProjectionError(
                        "{0}.{1} candidate ref is contradictory".format(
                            label, nested_field
                        )
                    )
                nested_occurrence = nested.get("semantic_occurrence_id")
                if nested_occurrence not in (None, "", occurrence):
                    raise SeedProjectionError(
                        "{0}.{1} semantic occurrence is contradictory".format(
                            label, nested_field
                        )
                    )
                continue
            _validate_nested_role(
                nested,
                node_ref=node_ref,
                occurrence=occurrence,
                role=role,
                label="{0}.{1}".format(label, nested_field),
            )
            _add_record(
                records,
                owner=owner,
                occurrence=occurrence,
                role=role,
                node_ref=node_ref,
                source_section=section,
            )


def _project_introductions(
    report: Mapping[str, Any],
    known_seed_bindings: Iterable[str],
    records: Dict[Tuple[str, str, str, str], ProjectedRoleOccurrence],
) -> None:
    for index, raw_candidate in enumerate(
        _items(
            report.get("introduction_candidates"),
            "report.introduction_candidates",
        )
    ):
        label = "report.introduction_candidates[{0}]".format(index)
        candidate = _mapping(raw_candidate, label)
        _required_string(candidate, "ref", label)
        _occurrence(candidate, label)

    for index, raw_step in enumerate(
        _items(report.get("step_judgments"), "report.step_judgments")
    ):
        label = "report.step_judgments[{0}]".format(index)
        step = _mapping(raw_step, label)
        if step.get("candidate_introduction") is not True:
            continue
        node_ref = _required_string(step, "current_node_ref", label)
        occurrence = _occurrence(step, label)
        owner_value = _mapping(step.get("owner"), "{0}.owner".format(label))
        owner = _owned_binding(
            step,
            owner_value,
            known_seed_bindings,
            label,
        )
        _add_record(
            records,
            owner=owner,
            occurrence=occurrence,
            role="introduction_candidate",
            node_ref=node_ref,
            source_section="step_judgments",
        )


def _validate_final_role_consistency(
    records: Iterable[ProjectedRoleOccurrence],
) -> None:
    roles_by_occurrence: Dict[Tuple[str, str], str] = {}
    roles_by_node: Dict[Tuple[str, str], str] = {}
    for record in records:
        if record.role == "introduction_candidate":
            continue
        for key, index in (
            (
                (record.seed_binding_identity, record.semantic_occurrence_id),
                roles_by_occurrence,
            ),
            ((record.seed_binding_identity, record.node_ref), roles_by_node),
        ):
            prior = index.get(key)
            if prior is not None and prior != record.role:
                raise SeedProjectionError(
                    "one seed-owned fact has contradictory causal roles"
                )
            index[key] = record.role


def _project_seed_owned_report(
    report_value: Mapping[str, Any],
    labels_value: Mapping[str, Any],
) -> SeedOwnedReportProjection:
    report = _mapping(report_value, "report")
    label_seeds = _validated_label_seeds(labels_value)
    _validate_report_seed_coverage(report, label_seeds)
    records_by_key: Dict[
        Tuple[str, str, str, str], ProjectedRoleOccurrence
    ] = {}
    _project_role_sections(
        report,
        label_seeds,
        records_by_key,
    )
    _project_introductions(
        report,
        label_seeds,
        records_by_key,
    )
    _validate_final_role_consistency(records_by_key.values())
    records = tuple(sorted(records_by_key.values()))
    seeds = tuple(
        SeedProjection(
            seed_binding_identity=identity,
            records=tuple(
                item for item in records if item.seed_binding_identity == identity
            ),
        )
        for identity in sorted(label_seeds)
    )
    return SeedOwnedReportProjection(records=records, seeds=seeds)


def project_seed_owned_report(
    report: Mapping[str, Any],
    labels: Mapping[str, Any],
) -> SeedOwnedReportProjection:
    """Project annotated v5 report publications into immutable seed-owned roles."""
    try:
        return _project_seed_owned_report(report, labels)
    except SeedProjectionError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SeedProjectionError(str(error)) from error


_SCORED_ROLE_FIELDS = (
    ("roots", "root"),
    ("conditions", "contributing_condition"),
    ("amplifiers", "amplifying_factor"),
    ("materializations", "downstream_materialization"),
    ("unrelated", "unrelated"),
)
_POSITIVE_ROLES = frozenset(
    {
        "root",
        "contributing_condition",
        "amplifying_factor",
        "downstream_materialization",
    }
)
_REPORT_SEED_OUTCOMES = frozenset(
    {"confirmed_root", "no_defect", "evidence_gap", "inconclusive"}
)


def _rounded_ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _classification_metrics(
    expected: Set[Tuple[str, ...]],
    predicted: Set[Tuple[str, ...]],
) -> ClassificationMetrics:
    tp = len(expected.intersection(predicted))
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    return ClassificationMetrics(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=_rounded_ratio(tp, tp + fp),
        recall=_rounded_ratio(tp, tp + fn),
        f1=_rounded_ratio(2 * tp, 2 * tp + fp + fn),
    )


def _macro_metric(values: Iterable[Optional[float]]) -> MacroMetric:
    applicable = tuple(value for value in values if value is not None)
    return MacroMetric(
        value=(
            round(sum(applicable) / len(applicable), 6)
            if applicable
            else None
        ),
        contributor_count=len(applicable),
    )


def _coverage(covered: int, declared: int) -> CoverageMetric:
    value = 1.0 if declared == 0 else round(covered / declared, 6)
    return CoverageMetric(covered=covered, declared=declared, value=value)


def _strict_string_items(value: Any, label: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    items = _items(value, label)
    if any(not isinstance(item, str) or not item for item in items):
        raise SeedProjectionError(
            "{0} must contain non-empty strings".format(label)
        )
    if len(items) != len(set(items)):
        raise SeedProjectionError("{0} contains duplicates".format(label))
    return tuple(items)


def _expected_role_sets(
    labels: Mapping[str, Any],
    label_seeds: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Set[Tuple[str, str]]]:
    expected: Dict[str, Set[Tuple[str, str]]] = {
        identity: set() for identity in label_seeds
    }
    for identity, seed in label_seeds.items():
        for field, role in _SCORED_ROLE_FIELDS:
            for entry in seed[field]:
                expected[identity].add(
                    (str(entry["semantic_occurrence_id"]), role)
                )
    for factor in labels["case_shared_factors"]:
        fact = (
            str(factor["semantic_occurrence_id"]),
            str(factor["factor_role"]),
        )
        for identity in factor["seed_binding_identities"]:
            expected[str(identity)].add(fact)
    return expected


def _actual_role_sets(
    projection: SeedOwnedReportProjection,
) -> Dict[str, Set[Tuple[str, str]]]:
    actual: Dict[str, Set[Tuple[str, str]]] = {
        seed.seed_binding_identity: set() for seed in projection.seeds
    }
    for record in projection.records:
        if record.role == "introduction_candidate":
            continue
        actual[record.seed_binding_identity].add(
            (record.semantic_occurrence_id, record.role)
        )
    return actual


def _first_root_occurrences(
    report: Mapping[str, Any],
    known_seed_bindings: Iterable[str],
) -> Dict[str, str]:
    first: Dict[str, str] = {}
    for section in ("confirmed_roots", "co_roots"):
        for index, raw_publication in enumerate(
            _items(report.get(section), "report.{0}".format(section))
        ):
            label = "report.{0}[{1}]".format(section, index)
            publication = _mapping(raw_publication, label)
            confirmation = _mapping(
                publication.get("confirmation"),
                "{0}.confirmation".format(label),
            )
            owner = _owned_binding(
                publication,
                confirmation,
                known_seed_bindings,
                label,
            )
            first.setdefault(owner, _occurrence(publication, label))
    return first


def _expected_global_outcome(outcomes: Sequence[str]) -> Optional[str]:
    if not outcomes or any(item not in _REPORT_SEED_OUTCOMES for item in outcomes):
        return None
    if all(item == "no_defect" for item in outcomes):
        return "no_defect"
    if all(item in {"confirmed_root", "no_defect"} for item in outcomes) and (
        "confirmed_root" in outcomes
    ):
        return "confirmed_root"
    if "inconclusive" not in outcomes and len(set(outcomes)) > 1:
        return "partial"
    return "inconclusive"


def _unresolved_outcome_tokens(
    report: Mapping[str, Any],
    report_outcome: str,
) -> Tuple[str, ...]:
    if report_outcome not in {"evidence_gap", "inconclusive"}:
        return ()
    tokens = {"inconclusive"}
    if report.get("analysis_outcome") == "partial":
        tokens.update({"partial", "partial_root_found"})
    return tuple(sorted(tokens))


def _score_seed_owned_report(
    report_value: Mapping[str, Any],
    labels_value: Mapping[str, Any],
    *,
    bound_seed_binding_identities: Iterable[str],
    binding_safety_passed: bool,
    checkpoint_safety_passed: bool,
) -> SeedOwnedMetrics:
    if type(binding_safety_passed) is not bool:
        raise SeedProjectionError("binding_safety_passed must be boolean")
    if type(checkpoint_safety_passed) is not bool:
        raise SeedProjectionError("checkpoint_safety_passed must be boolean")
    report = _mapping(report_value, "report")
    try:
        labels = validate_labels(labels_value)
    except (TypeError, ValueError) as error:
        raise SeedProjectionError("invalid labels: {0}".format(error)) from error
    if labels.get("schema_version") != V5_LABEL_SCHEMA_VERSION:
        raise SeedProjectionError("seed metrics require v5 labels")
    if report.get("case_id") != labels["case_id"]:
        raise SeedProjectionError("report and labels case_id mismatch")

    label_seeds = {
        str(seed["seed_binding_identity"]): seed for seed in labels["seeds"]
    }
    projection = project_seed_owned_report(report, labels)
    expected_roles = _expected_role_sets(labels, label_seeds)
    actual_roles = _actual_role_sets(projection)
    first_roots = _first_root_occurrences(report, label_seeds)

    raw_bound = tuple(bound_seed_binding_identities)
    if any(not isinstance(identity, str) or not identity for identity in raw_bound):
        raise SeedProjectionError(
            "bound_seed_binding_identities must contain non-empty strings"
        )
    if len(raw_bound) != len(set(raw_bound)):
        raise SeedProjectionError("bound seed identities contain duplicates")
    bound = set(raw_bound)
    if not bound.issubset(set(label_seeds)):
        raise SeedProjectionError("bound seed identities contain an unknown seed")

    report_results: Dict[str, Mapping[str, Any]] = {}
    report_seed_items = _items(report.get("seed_results"), "report.seed_results")
    for index, raw_result in enumerate(report_seed_items):
        result = _mapping(raw_result, "report.seed_results[{0}]".format(index))
        identity = _required_string(
            result,
            "seed_binding_identity",
            "report.seed_results[{0}]".format(index),
        )
        report_results[identity] = result

    report_outcomes = [
        str(report_results[identity].get("outcome") or "")
        for identity in sorted(label_seeds)
    ]
    expected_global_outcome = _expected_global_outcome(report_outcomes)
    global_terminal_state_valid = (
        expected_global_outcome is not None
        and report.get("analysis_outcome") == expected_global_outcome
    )

    per_seed: List[SeedMetricResult] = []
    all_expected_root_tuples: Set[Tuple[str, ...]] = set()
    all_actual_root_tuples: Set[Tuple[str, ...]] = set()
    all_expected_role_tuples: Set[Tuple[str, ...]] = set()
    all_actual_role_tuples: Set[Tuple[str, ...]] = set()
    for identity in sorted(label_seeds):
        seed = label_seeds[identity]
        result = report_results[identity]
        expected = expected_roles[identity]
        actual = actual_roles[identity]
        expected_roots = {
            (occurrence,) for occurrence, role in expected if role == "root"
        }
        actual_roots = {
            (occurrence,) for occurrence, role in actual if role == "root"
        }
        root_metrics = _classification_metrics(expected_roots, actual_roots)
        role_pair_metrics = _classification_metrics(
            {(occurrence, role) for occurrence, role in expected},
            {(occurrence, role) for occurrence, role in actual},
        )
        top1_match = (
            bool(
                first_roots.get(identity)
                and (first_roots[identity],) in expected_roots
            )
            if seed["expected_outcome"] == "confirmed_root"
            else None
        )

        report_outcome = str(result.get("outcome") or "")
        root_refs = _strict_string_items(
            result.get("confirmed_root_refs"),
            "report seed confirmed_root_refs",
        )
        missing = _strict_string_items(
            result.get("missing_evidence"), "report seed missing_evidence"
        )
        blocking = _strict_string_items(
            result.get("blocking_reasons"), "report seed blocking_reasons"
        )
        positive_roles = {
            (occurrence, role)
            for occurrence, role in actual
            if role in _POSITIVE_ROLES
        }
        actual_root_refs = {
            record.node_ref
            for record in projection.for_seed(identity).records
            if record.role == "root"
        }
        local_terminal_valid = report_outcome in _REPORT_SEED_OUTCOMES
        if report_outcome == "confirmed_root":
            local_terminal_valid = (
                local_terminal_valid
                and bool(actual_roots)
                and set(root_refs) == actual_root_refs
                and not missing
                and not blocking
            )
            terminal_outcome = "confirmed_root"
        elif report_outcome == "no_defect":
            local_terminal_valid = (
                local_terminal_valid
                and not positive_roles
                and not root_refs
                and not missing
                and not blocking
            )
            terminal_outcome = "no_defect"
        elif report_outcome in {"evidence_gap", "inconclusive"}:
            local_terminal_valid = (
                local_terminal_valid
                and not positive_roles
                and not root_refs
                and (report_outcome != "evidence_gap" or bool(missing))
            )
            terminal_outcome = "unresolved"
        else:
            terminal_outcome = "ambiguous"
        terminal_valid = local_terminal_valid and global_terminal_state_valid

        unresolved_tokens = _unresolved_outcome_tokens(report, report_outcome)
        matched_unresolved = next(
            (
                allowed
                for allowed in seed["allowed_unresolved_outcomes"]
                if allowed in unresolved_tokens
            ),
            None,
        )
        outcome_agreement = bool(
            terminal_valid
            and terminal_outcome == seed["expected_outcome"]
            and (
                seed["expected_outcome"] != "unresolved"
                or matched_unresolved is not None
            )
        )
        forbidden_expected = {
            str(entry["semantic_occurrence_id"])
            for entry in seed["forbidden_roots"]
        }
        forbidden_published = tuple(
            sorted(
                occurrence
                for occurrence, role in actual
                if role == "root" and occurrence in forbidden_expected
            )
        )

        per_seed.append(
            SeedMetricResult(
                seed_binding_identity=identity,
                defect_id=str(seed["defect_id"]),
                expected_outcome=str(seed["expected_outcome"]),
                report_outcome=report_outcome,
                terminal_outcome=terminal_outcome,
                matched_unresolved_outcome=matched_unresolved,
                unresolved_reasons=tuple(sorted({*missing, *blocking})),
                root_metrics=root_metrics,
                top1_match=top1_match,
                role_pair_metrics=role_pair_metrics,
                outcome_agreement=outcome_agreement,
                terminal_valid=terminal_valid,
                forbidden_root_passed=not forbidden_published,
                forbidden_root_occurrences=forbidden_published,
            )
        )
        all_expected_root_tuples.update(
            (identity, occurrence) for (occurrence,) in expected_roots
        )
        all_actual_root_tuples.update(
            (identity, occurrence) for (occurrence,) in actual_roots
        )
        all_expected_role_tuples.update(
            (identity, occurrence, role) for occurrence, role in expected
        )
        all_actual_role_tuples.update(
            (identity, occurrence, role) for occurrence, role in actual
        )

    declared = len(label_seeds)
    label_coverage = _coverage(len(bound), declared)
    report_coverage = _coverage(len(report_results), declared)
    terminal_coverage = _coverage(
        sum(1 for item in per_seed if item.terminal_valid), declared
    )
    macro = (
        ("root_precision", _macro_metric(item.root_metrics.precision for item in per_seed)),
        ("root_recall", _macro_metric(item.root_metrics.recall for item in per_seed)),
        ("root_f1", _macro_metric(item.root_metrics.f1 for item in per_seed)),
        (
            "top1_match",
            _macro_metric(
                float(item.top1_match) if item.top1_match is not None else None
                for item in per_seed
            ),
        ),
        (
            "role_pair_precision",
            _macro_metric(item.role_pair_metrics.precision for item in per_seed),
        ),
        (
            "role_pair_recall",
            _macro_metric(item.role_pair_metrics.recall for item in per_seed),
        ),
        (
            "role_pair_f1",
            _macro_metric(item.role_pair_metrics.f1 for item in per_seed),
        ),
        (
            "outcome_agreement",
            _macro_metric(float(item.outcome_agreement) for item in per_seed),
        ),
    )
    all_passed = bool(
        label_coverage.value == 1.0
        and report_coverage.value == 1.0
        and terminal_coverage.value == 1.0
        and binding_safety_passed
        and checkpoint_safety_passed
        and global_terminal_state_valid
        and all(item.outcome_agreement for item in per_seed)
        and all(item.forbidden_root_passed for item in per_seed)
    )
    return SeedOwnedMetrics(
        case_id=str(labels["case_id"]),
        per_seed=tuple(per_seed),
        micro_root_metrics=_classification_metrics(
            all_expected_root_tuples, all_actual_root_tuples
        ),
        micro_role_pair_metrics=_classification_metrics(
            all_expected_role_tuples, all_actual_role_tuples
        ),
        macro_metrics=macro,
        label_seed_coverage=label_coverage,
        report_seed_coverage=report_coverage,
        terminal_seed_coverage=terminal_coverage,
        binding_safety_passed=binding_safety_passed,
        checkpoint_safety_passed=checkpoint_safety_passed,
        global_terminal_state_valid=global_terminal_state_valid,
        all_seed_coverage_passed=all_passed,
    )


def score_seed_owned_report(
    report: Mapping[str, Any],
    labels: Mapping[str, Any],
    *,
    bound_seed_binding_identities: Iterable[str],
    binding_safety_passed: bool = True,
    checkpoint_safety_passed: bool = True,
) -> SeedOwnedMetrics:
    """Score a validated v5 report by exact seed-owned occurrence identities."""
    try:
        return _score_seed_owned_report(
            report,
            labels,
            bound_seed_binding_identities=bound_seed_binding_identities,
            binding_safety_passed=binding_safety_passed,
            checkpoint_safety_passed=checkpoint_safety_passed,
        )
    except SeedProjectionError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SeedProjectionError(str(error)) from error


ProjectionRecord = ProjectedRoleOccurrence
project_report_by_seed = project_seed_owned_report


__all__ = [
    "COMPARISON_V8_SCHEMA_VERSION",
    "ClassificationMetrics",
    "CoverageMetric",
    "MacroMetric",
    "ProjectedRoleOccurrence",
    "ProjectionRecord",
    "SeedMetricResult",
    "SeedOwnedMetrics",
    "SeedOwnedReportProjection",
    "SeedProjection",
    "SeedProjectionError",
    "project_report_by_seed",
    "project_seed_owned_report",
    "score_seed_owned_report",
]
