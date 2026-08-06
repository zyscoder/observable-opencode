"""Shared offline attribution orchestration for CLI and Python callers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .analyzer import BackwardTaintAnalyzer
from .benchmark_case import load_benchmark_case
from .cache import JudgmentCache
from .causal_judge import ClaudeCausalJudge
from .causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    annotate_report_semantic_anchors,
)
from .checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
    publish_output_transaction,
)
from .claude import ClaudeJudgeClient
from .errors import AttributionInputError
from .graph import TraceGraph
from .models import stable_json
from .recursive_analyzer import AgenticRecursiveAnalyzer
from .request import (
    QUESTION_SCHEMA_VERSION,
    AttributionQuestion,
    AttributionRequest,
    AttributionResult,
)


class GracefulSignalState:
    """Signal-safe stop flag shared by SIGINT and SIGTERM."""

    def __init__(self) -> None:
        self._requested = False
        self.signal_name = ""
        self._previous: dict[int, Any] = {}

    def handle(self, signum: int, _frame: Any) -> None:
        self._requested = True
        try:
            self.signal_name = signal.Signals(signum).name
        except ValueError:
            self.signal_name = str(signum)

    def stop_requested(self) -> bool:
        return self._requested

    def __enter__(self) -> "GracefulSignalState":
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self.handle)
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()


def analyze(request: AttributionRequest) -> AttributionResult:
    """Run offline attribution and publish its report and lineage outputs."""
    if not isinstance(request, AttributionRequest):
        raise TypeError("request must be an AttributionRequest")

    paths = validate_request_path_isolation(request)
    graph = load_graph(
        request.trace_path,
        request.review_path,
        request.evaluation_paths,
        bundle_path=request.benchmark_bundle_path,
    )
    starts = analysis_start_refs(
        graph,
        request.start_refs,
        question=request.normalized_question,
    )
    options = request.options
    objective = request.effective_objective
    out = request.output_path
    cache_path = paths["judge_cache"]
    transport = ClaudeJudgeClient(
        model=options.model,
        api_key_env=options.api_key_env,
        base_url=options.base_url,
        base_url_env=options.base_url_env,
        max_tokens=options.judge_max_tokens,
        timeout_seconds=options.judge_timeout_sec,
        thinking_mode=options.thinking_mode,
        cache_path=str(cache_path),
        provider_error_threshold=options.provider_error_threshold,
    )
    lineage_out = paths["lineage"]

    if options.engine == "recursive-agentic":
        report_payload = _run_recursive_analysis(
            request=request,
            graph=graph,
            starts=starts,
            objective=objective,
            transport=transport,
            lineage_out=lineage_out,
            cache_path=cache_path,
            checkpoint_path=paths["checkpoint"],
        )
    else:
        report = BackwardTaintAnalyzer(
            judge=transport,
            max_depth=options.effective_max_depth,
            max_nodes=options.max_nodes,
        ).analyze(
            graph,
            start_refs=starts,
            objective=objective,
        )
        report_payload = question_bound_output_payload(
            attribution_output_payload(report, graph),
            graph,
            binding=request.question_binding,
            starts=starts,
        )
        atomic_write_json(out, report_payload)
        atomic_write_json(lineage_out, graph.message_lineage)

    return AttributionResult(
        output_path=out,
        lineage_path=lineage_out,
        payload=report_payload,
        question=request.question_binding,
    )


def _run_recursive_analysis(
    *,
    request: AttributionRequest,
    graph: TraceGraph,
    starts: tuple[str, ...],
    objective: str,
    transport: ClaudeJudgeClient,
    lineage_out: Path,
    cache_path: Path,
    checkpoint_path: Path,
) -> dict[str, Any]:
    options = request.options
    out = request.output_path
    budgets = {
        "max_frontier_items": options.max_frontier_items,
        "max_depth": options.effective_max_depth,
        "max_hypotheses": options.max_hypotheses,
        "max_investigation_rounds": options.max_investigation_rounds,
        "max_artifact_bytes": options.max_artifact_bytes,
        "max_judge_requests": options.max_judge_requests,
    }
    model_identity = stable_json(
        {
            "model": transport.model,
            "base_url": transport.base_url,
            "thinking": transport.thinking_config,
            "max_tokens": transport.max_tokens,
            "provider_error_threshold": transport.provider_error_threshold,
            "fusion_mode": options.fusion_mode,
            "global_judgment_contract": GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            "root_confirmation_contract": ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        }
    )
    checkpoint_config = build_checkpoint_config(
        trace=graph.raw_trace,
        case_id=graph.case_id,
        objective=objective,
        analysis_perspective=options.analysis_perspective,
        start_refs=starts,
        budgets=budgets,
        model_identity=model_identity,
        cache_identity=str(cache_path.expanduser().resolve()),
        runtime_identity={
            "judge_timeout_sec": options.judge_timeout_sec,
            "judge_max_tokens": transport.max_tokens,
            "thinking_mode": stable_json(transport.thinking_config),
            "base_url": transport.base_url,
            "provider_error_threshold": transport.provider_error_threshold,
            "fusion_mode": options.fusion_mode,
        },
    )
    causal_judge = ClaudeCausalJudge(
        transport=transport,
        cache=(
            transport.cache
            if isinstance(transport.cache, JudgmentCache)
            else JudgmentCache(cache_path)
        ),
    )
    checkpoint = CheckpointBundle(checkpoint_path)
    with GracefulSignalState() as shutdown:
        report = AgenticRecursiveAnalyzer(
            judge=causal_judge,
            max_frontier_items=options.max_frontier_items,
            max_depth=options.effective_max_depth,
            max_hypotheses=options.max_hypotheses,
            max_investigation_rounds=options.max_investigation_rounds,
            max_artifact_bytes=options.max_artifact_bytes,
            max_judge_requests=options.max_judge_requests,
            checkpoint=checkpoint,
            checkpoint_config=checkpoint_config,
            stop_requested=shutdown.stop_requested,
            fusion_mode=options.fusion_mode,
        ).analyze(
            graph,
            start_refs=starts,
            objective=objective,
            analysis_perspective=options.analysis_perspective,
        )
        report_payload = question_bound_output_payload(
            attribution_output_payload(report, graph),
            graph,
            binding=request.question_binding,
            starts=starts,
        )
        output_commit = checkpoint.completed_replay_output_commit(
            attribution_path=out,
            lineage_path=lineage_out,
            report=report_payload,
            message_lineage=graph.message_lineage,
        )
        if output_commit is None:
            output_commit = publish_output_transaction(
                bundle=checkpoint,
                attribution_path=out,
                lineage_path=lineage_out,
                report=report_payload,
                message_lineage=graph.message_lineage,
                stop_requested=shutdown.stop_requested,
            )
            if report.metadata.get("termination_reason") == "signal_interrupted":
                checkpoint.record_action(
                    "analysis_interrupted",
                    "analysis:result",
                    {
                        "report": report_payload,
                        "interrupted": True,
                        "output_transaction_id": output_commit["transaction_id"],
                        "output_commit_hash": output_commit["output_commit_hash"],
                    },
                )
            else:
                checkpoint.mark_analysis_completed(
                    report=report_payload,
                    output_commit=output_commit,
                )
    return report_payload


def analysis_start_refs(
    graph: TraceGraph,
    explicit_refs: Iterable[str],
    *,
    question: str = "",
) -> tuple[str, ...]:
    requested = tuple(explicit_refs)
    if not requested:
        defaults = tuple(graph.default_start_refs())
        question_tokens = _normalized_tokens(question)
        if not question_tokens:
            return defaults
        indexed = list(enumerate(defaults))
        indexed.sort(
            key=lambda item: (
                -_start_relevance(graph, item[1], question_tokens),
                item[0],
            )
        )
        return tuple(ref for _, ref in indexed)

    resolved = tuple(graph.resolve(ref) or ref for ref in requested)
    for requested_ref, resolved_ref in zip(requested, resolved):
        node = graph.nodes.get(resolved_ref)
        if node is None or graph.analysis_start_eligible(resolved_ref):
            continue
        raise AttributionInputError(
            "--start-ref {0} resolves to an external evaluation fact that is ineligible for decisive judgment".format(
                requested_ref
            )
        )
    return resolved


def _normalized_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = set(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", normalized):
        tokens.update(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(tokens)


def _start_relevance(
    graph: TraceGraph,
    ref: str,
    question_tokens: frozenset[str],
) -> int:
    node = graph.nodes.get(ref)
    if node is None:
        return 0
    summary = node.data.get("summary")
    fields = (
        node.title,
        summary if isinstance(summary, str) else "",
        node.component,
        node.event_type,
    )
    return len(question_tokens.intersection(_normalized_tokens(" ".join(fields))))


def attribution_output_payload(report: Any, graph: TraceGraph) -> dict[str, Any]:
    return annotate_report_semantic_anchors(
        graph.case_id,
        graph.nodes,
        report.to_dict(),
        graph=graph,
    )


def question_bound_output_payload(
    report: Mapping[str, Any],
    graph: TraceGraph,
    *,
    binding: AttributionQuestion | None,
    starts: Iterable[str],
) -> dict[str, Any]:
    """Attach a read-only question projection without changing objective reports."""
    payload = _json_safe_copy(report)
    selected_starts = tuple(starts)
    if binding is None:
        return payload

    payload.update(
        {
            "analysis_question": {
                "schema_version": QUESTION_SCHEMA_VERSION,
                "question": binding.original,
                "normalized_question": binding.normalized,
                "question_id": binding.question_id,
                "trace_binding": "sha256:{0}".format(
                    hashlib.sha256(stable_json(graph.raw_trace).encode("utf-8")).hexdigest()
                ),
                "selected_start_refs": list(selected_starts),
                "analysis_mode": "offline_read_only",
            },
            **_question_report_projection(payload, graph, starts=selected_starts),
        }
    )
    return payload


def _question_report_projection(
    report: Mapping[str, Any], graph: TraceGraph, *, starts: Iterable[str]
) -> dict[str, Any]:
    confirmed_roots = _question_roots(report)
    evidence_refs, evidence_gaps, supported_root_indexes = _root_evidence_projection(
        confirmed_roots,
        graph,
    )
    rootless_gaps = (
        []
        if confirmed_roots
        else [
            {
                "kind": "no_confirmed_root_cause",
                "reason": "evidence_insufficient",
            }
        ]
    )
    outcome = str(report.get("analysis_outcome") or "inconclusive")
    return {
        "conclusion": _question_conclusion(confirmed_roots, outcome),
        "causal_chain": _taint_paths(
            report.get("taint_paths"),
            starts=starts,
            roots=confirmed_roots,
        ),
        "supporting_evidence_refs": evidence_refs,
        "rejected_hypotheses": _rejected_hypotheses(report),
        "confidence": _root_confidence(
            confirmed_roots,
            supported_root_indexes=supported_root_indexes,
        ),
        "unresolved_gaps": _dedupe_report_values(
            [*_unresolved_gaps(report), *evidence_gaps, *rootless_gaps]
        ),
    }


def _question_roots(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    roots: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in (
        *_mapping_items(report.get("confirmed_roots")),
        *_mapping_items(report.get("co_roots")),
        *_mapping_items(report.get("root_causes")),
    ):
        key = (str(root.get("node_ref") or ""), _root_identity(root))
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _root_identity(root: Mapping[str, Any]) -> str:
    occurrence = root.get("semantic_occurrence_id")
    if isinstance(occurrence, str) and occurrence:
        return occurrence
    direct = root.get("confirmation_identity")
    if isinstance(direct, str) and direct:
        return direct
    confirmation = root.get("confirmation")
    if isinstance(confirmation, Mapping):
        identity = confirmation.get("confirmation_identity")
        if isinstance(identity, str) and identity:
            return identity
    return ""


def _question_conclusion(roots: Iterable[Mapping[str, Any]], outcome: str) -> str:
    roots = list(roots)
    if not roots:
        return "{0}: no confirmed root cause; evidence is insufficient.".format(outcome)
    return "\n".join(
        "{0}: {1}".format(
            str(root.get("node_ref") or ""),
            _root_reason(root) or "no recorded root reason.",
        )
        for root in roots
    )


def _root_reason(root: Mapping[str, Any]) -> str:
    reason = str(root.get("reason") or "")
    if not reason:
        defect_state = root.get("defect_state")
        if isinstance(defect_state, Mapping):
            reason = str(defect_state.get("mechanism") or "")
    return reason


def _taint_paths(
    value: Any,
    *,
    starts: Iterable[str],
    roots: Iterable[Mapping[str, Any]],
) -> list[list[Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    start_refs = {str(ref) for ref in starts if isinstance(ref, str) and ref}
    root_refs = {
        str(root.get("node_ref") or "")
        for root in roots
        if str(root.get("node_ref") or "")
    }
    output: list[list[Any]] = []
    for path in value:
        if not isinstance(path, (list, tuple)):
            continue
        path_refs = {ref for ref in path if isinstance(ref, str)}
        if not path_refs.intersection(start_refs):
            continue
        if root_refs and not path_refs.intersection(root_refs):
            continue
        output.append(_json_safe_copy(path))
    return output


def _root_evidence_projection(
    roots: Iterable[Mapping[str, Any]], graph: TraceGraph
) -> tuple[list[str], list[dict[str, Any]], frozenset[int]]:
    refs: list[str] = []
    gaps: list[dict[str, Any]] = []
    supported_root_indexes: set[int] = set()
    for index, root in enumerate(roots):
        root_ref = str(root.get("node_ref") or "")
        eligible_for_root: list[str] = []
        for ref in _root_evidence_refs(root):
            if (
                not isinstance(ref, str)
                or not ref
                or not graph.active_revision_evidence_reference_eligible(ref)
            ):
                if isinstance(ref, str) and ref:
                    gaps.append(
                        {
                            "kind": "root_evidence_ref_rejected",
                            "root_ref": root_ref,
                            "ref": ref,
                            "reason": "missing_or_ineligible",
                        }
                    )
                continue
            refs.append(ref)
            eligible_for_root.append(ref)
        if eligible_for_root:
            supported_root_indexes.add(index)
        else:
            gaps.append(
                {
                    "kind": "root_without_eligible_supporting_evidence",
                    "root_ref": root_ref,
                }
            )
    return (
        _dedupe_report_values(refs),
        _dedupe_report_values(gaps),
        frozenset(supported_root_indexes),
    )


def _root_evidence_refs(root: Mapping[str, Any]) -> list[Any]:
    refs: list[Any] = []
    for field in ("evidence_refs", "observed_defect_refs"):
        value = root.get(field)
        if isinstance(value, (list, tuple)):
            refs.extend(value)
    return _dedupe_report_values(refs)


def _rejected_hypotheses(report: Mapping[str, Any]) -> list[Any]:
    rejected = list(_mapping_items(report.get("rejected_candidates")))
    rejected.extend(
        item
        for item in _mapping_items(report.get("hypotheses"))
        if item.get("status") == "rejected"
    )
    return _dedupe_report_values(rejected)


def _root_confidence(
    roots: Iterable[Mapping[str, Any]], *, supported_root_indexes: frozenset[int]
) -> float:
    values = [
        float(root["confidence"])
        for index, root in enumerate(roots)
        if index in supported_root_indexes
        if not isinstance(root.get("confidence"), bool)
        and isinstance(root.get("confidence"), (int, float))
    ]
    return max(values, default=0.0)


def _unresolved_gaps(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = [
        {"kind": "unresolved_ref", "ref": ref}
        for ref in report.get("unresolved_refs") or ()
        if isinstance(ref, str) and ref
    ]
    trace_report = report.get("trace_improvement_report")
    if not isinstance(trace_report, Mapping):
        metadata = report.get("metadata")
        trace_report = (
            metadata.get("trace_improvement_report")
            if isinstance(metadata, Mapping)
            else None
        )
    if isinstance(trace_report, Mapping):
        for key in ("blocking_gaps", "analysis_gaps", "advisory_gaps"):
            gaps.extend(
                {
                    "kind": "trace_improvement_gap",
                    "category": key,
                    "detail": _json_safe_copy(gap),
                }
                for gap in _mapping_items(trace_report.get(key))
            )
    return _dedupe_report_values(gaps)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dedupe_report_values(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        identity = stable_json(value)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return result


def _json_safe_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(item) for item in value]
    return value


def lineage_output_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(
        "{0}.message-lineage.json".format(attribution_out.stem)
    )


def judge_cache_output_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(
        "{0}.judge-cache.jsonl".format(attribution_out.stem)
    )


def recursive_checkpoint_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name("{0}.checkpoint".format(attribution_out.stem))


def validate_request_path_isolation(request: AttributionRequest) -> dict[str, Path]:
    """Resolve all writable paths and reject aliases before any analysis side effect."""
    output = request.output_path.resolve()
    lineage = lineage_output_path(output, request.options.lineage_output_path).resolve()
    judge_cache = judge_cache_output_path(output, request.options.judge_cache_path).resolve()
    writable = {
        "output": output,
        "lineage": lineage,
        "judge_cache": judge_cache,
    }
    if request.options.engine == "recursive-agentic":
        writable["checkpoint"] = recursive_checkpoint_path(
            output, request.options.checkpoint_path
        ).resolve()
    items = tuple(writable.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if left_path == right_path:
                raise AttributionInputError(
                    "writable path alias: {0} and {1} both resolve to {2}".format(
                        left_name, right_name, left_path
                    )
                )
            if _same_existing_filesystem_object(left_path, right_path):
                raise AttributionInputError(
                    "writable filesystem alias: {0} and {1} resolve to the same file".format(
                        left_name, right_name
                    )
                )
            if _path_is_within_filesystem(
                left_path, right_path
            ) or _path_is_within_filesystem(right_path, left_path):
                raise AttributionInputError(
                    "writable path containment collision: {0} resolves to {1}; {2} resolves to {3}".format(
                        left_name,
                        left_path,
                        right_name,
                        right_path,
                    )
                )

    inputs = {
        "trace": request.trace_path,
        "benchmark_bundle": request.benchmark_bundle_path,
        "review": request.review_path,
        **{
            "evaluation[{0}]".format(index): path
            for index, path in enumerate(request.evaluation_paths)
        },
    }
    for writable_name, writable_path in items:
        for input_name, input_path in inputs.items():
            if input_path is None:
                continue
            resolved_input = input_path.resolve()
            if writable_path == resolved_input:
                raise AttributionInputError(
                    "writable path aliases input: {0} and {1} both resolve to {2}".format(
                        writable_name, input_name, writable_path
                    )
                )
            if _same_existing_filesystem_object(writable_path, resolved_input):
                raise AttributionInputError(
                    "writable filesystem alias: {0} and {1} resolve to the same file".format(
                        writable_name, input_name
                    )
                )

    bundle = request.benchmark_bundle_path
    if bundle is not None and bundle.is_dir():
        resolved_bundle = bundle.resolve()
        for writable_name, writable_path in items:
            if _path_is_within_filesystem(writable_path, resolved_bundle):
                raise AttributionInputError(
                    "writable path is inside benchmark bundle input: {0} resolves to {1}".format(
                        writable_name, writable_path
                    )
                )

    checkpoint = writable.get("checkpoint")
    if checkpoint is not None and checkpoint.exists() and not checkpoint.is_dir():
        raise AttributionInputError(
            "recursive checkpoint directory resolves to an existing file: {0}".format(
                checkpoint
            )
        )
    return writable


def _path_is_within_filesystem(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        pass

    if _existing_directory_is_physical_ancestor(path, directory):
        return True

    path_ancestor, path_suffix = _deepest_existing_ancestor(path)
    directory_ancestor, directory_suffix = _deepest_existing_ancestor(directory)
    if not _same_existing_filesystem_object(path_ancestor, directory_ancestor):
        return False
    comparison_root = (
        path_ancestor
        if path_ancestor.is_dir()
        else path_ancestor.parent
    )
    if _filesystem_is_case_insensitive(comparison_root):
        path_suffix = tuple(part.casefold() for part in path_suffix)
        directory_suffix = tuple(part.casefold() for part in directory_suffix)
    return path_suffix[: len(directory_suffix)] == directory_suffix


def _existing_directory_is_physical_ancestor(path: Path, directory: Path) -> bool:
    try:
        if not directory.is_dir():
            return False
    except OSError:
        return False

    candidate, _ = _deepest_existing_ancestor(path)
    try:
        if not candidate.is_dir():
            candidate = candidate.parent
    except OSError:
        candidate = candidate.parent
    while True:
        if _same_existing_filesystem_object(candidate, directory):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def _same_existing_filesystem_object(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(str(left), str(right))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def _deepest_existing_ancestor(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = path
    suffix: list[str] = []
    while True:
        try:
            if candidate.exists():
                return candidate, tuple(reversed(suffix))
        except OSError:
            pass
        parent = candidate.parent
        if parent == candidate:
            return candidate, tuple(reversed(suffix))
        suffix.append(candidate.name)
        candidate = parent


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """Detect directory lookup semantics using only existing filesystem objects."""
    try:
        with os.scandir(str(directory)) as iterator:
            entries = tuple(iterator)
        entry_names = {entry.name for entry in entries}
        for entry in entries:
            if entry.is_symlink():
                continue
            alternate_name = _ascii_case_variant(entry.name)
            if alternate_name is None:
                continue
            if alternate_name in entry_names:
                return False
            return _same_existing_filesystem_object(
                Path(entry.path), directory / alternate_name
            )
    except OSError:
        pass

    candidate = directory
    while candidate.parent != candidate:
        alternate_name = _ascii_case_variant(candidate.name)
        if alternate_name is not None:
            return _same_existing_filesystem_object(
                candidate, candidate.with_name(alternate_name)
            )
        candidate = candidate.parent
    return False


def _ascii_case_variant(name: str) -> str | None:
    for index, character in enumerate(name):
        if "a" <= character <= "z":
            return name[:index] + character.upper() + name[index + 1 :]
        if "A" <= character <= "Z":
            return name[:index] + character.lower() + name[index + 1 :]
    return None


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a JSON output after fsyncing file and directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_graph(
    trace_path: Optional[Path],
    review_path: Optional[Path] = None,
    evaluation_paths: Iterable[Path] = (),
    *,
    bundle_path: Optional[Path] = None,
) -> TraceGraph:
    return load_benchmark_case(
        bundle_path=bundle_path,
        trace_path=trace_path,
        review_path=review_path,
        evaluation_paths=tuple(evaluation_paths),
        role="attribution",
    ).graph
