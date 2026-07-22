from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .analyzer import BackwardTaintAnalyzer
from .cache import JudgmentCache
from .causal_judge import ClaudeCausalJudge
from .causal_state import annotate_report_semantic_anchors
from .checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
    publish_output_transaction,
)
from .claude import ClaudeJudgeClient, default_judge_timeout_seconds
from .evaluation_facts import inject_external_evaluation_facts
from .graph import TraceGraph, artifact_root_for_trace_path
from .models import stable_json
from .quality_review import inject_quality_gap_records
from .recursive_analyzer import AgenticRecursiveAnalyzer


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


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run backward semantic taint attribution on trace.json.")
    parser.add_argument(
        "--engine",
        choices=("legacy", "recursive-agentic"),
        default="legacy",
        help="Attribution engine; legacy remains the compatibility default.",
    )
    parser.add_argument(
        "--fusion-mode",
        choices=("off", "retrieval-global"),
        default="retrieval-global",
        help="Recursive engine candidate strategy; retrieval-global compares evidence capsules before bounded recursive expansion.",
    )
    parser.add_argument("--trace", required=True, help="Path to observable-opencode trace.json")
    parser.add_argument("--review", default="", help="Optional trace-review JSON; quality gaps are injected as start nodes")
    parser.add_argument(
        "--evaluation",
        action="append",
        default=[],
        help="External benchmark evaluation fact JSON; repeatable.",
    )
    parser.add_argument("--out", required=True, help="Path to write attribution JSON report")
    parser.add_argument(
        "--lineage-out",
        default="",
        help="Optional message-lineage JSON path; defaults next to --out as <stem>.message-lineage.json",
    )
    parser.add_argument(
        "--judge-cache",
        default="",
        help="Optional judgment checkpoint path; defaults next to --out as <stem>.judge-cache.jsonl",
    )
    parser.add_argument("--objective", default="Find the root cause of the observed bad final result.")
    parser.add_argument(
        "--analysis-perspective",
        default="Find the best-supported causal explanation.",
        help="Recursive root ranking perspective; it never changes evidence eligibility.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default="",
        help="Recursive checkpoint directory; defaults to <out-stem>.checkpoint.",
    )
    parser.add_argument("--start-ref", action="append", default=[], help="Trace ref to start from; repeatable")
    parser.add_argument("--model", default="", help="Claude model id; defaults to CLAUDE_MODEL or claude-sonnet-4-5")
    parser.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    parser.add_argument("--base-url", default="", help="Anthropic-compatible API base URL; defaults to ANTHROPIC_BASE_URL")
    parser.add_argument("--base-url-env", default="ANTHROPIC_BASE_URL")
    parser.add_argument(
        "--judge-timeout-sec",
        type=float,
        default=default_judge_timeout_seconds(),
        help="Timeout in seconds for each judge/repair request; defaults to CLAUDE_TIMEOUT_SECONDS or 3600.",
    )
    parser.add_argument(
        "--judge-max-tokens",
        type=int,
        default=4096,
        help="Max output tokens for each judge call; reasoning models may need extra room for JSON text.",
    )
    parser.add_argument(
        "--thinking-mode",
        choices=("auto", "enabled", "disabled"),
        default="auto",
        help="Judge thinking mode. Auto disables thinking for the DeepSeek Anthropic endpoint.",
    )
    parser.add_argument("--max-depth", "--recursive-max-depth", dest="max_depth", type=int, default=None)
    parser.add_argument("--max-nodes", type=int, default=48)
    parser.add_argument("--max-frontier-items", type=int, default=96)
    parser.add_argument("--max-hypotheses", type=int, default=24)
    parser.add_argument("--max-investigation-rounds", type=int, default=12)
    parser.add_argument("--max-artifact-bytes", type=int, default=1_048_576)
    parser.add_argument("--max-judge-requests", type=int, default=128)
    parser.add_argument(
        "--provider-error-threshold",
        type=int,
        default=3,
        help="Open the provider circuit after this many consecutive connection/timeout errors.",
    )
    args = parser.parse_args(argv)
    if args.max_depth is None:
        args.max_depth = 20 if args.engine == "recursive-agentic" else 8
    args.recursive_max_depth = args.max_depth if args.engine == "recursive-agentic" else 20
    for name in (
        "max_depth",
        "max_nodes",
        "max_frontier_items",
        "max_hypotheses",
        "max_investigation_rounds",
        "max_artifact_bytes",
        "max_judge_requests",
    ):
        if getattr(args, name) < 0:
            parser.error("--{0} must be non-negative".format(name.replace("_", "-")))
    return args


def main() -> int:
    args = parse_args()
    graph = load_graph(
        Path(args.trace),
        Path(args.review) if args.review else None,
        [Path(path) for path in args.evaluation],
    )
    out = Path(args.out)
    cache_path = judge_cache_output_path(out, args.judge_cache)
    transport = ClaudeJudgeClient(
        model=args.model,
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        base_url_env=args.base_url_env,
        max_tokens=args.judge_max_tokens,
        timeout_seconds=args.judge_timeout_sec,
        thinking_mode=args.thinking_mode,
        cache_path=str(cache_path),
        provider_error_threshold=args.provider_error_threshold,
    )
    if args.engine == "recursive-agentic":
        checkpoint_path = recursive_checkpoint_path(out, args.checkpoint_dir)
        lineage_out = lineage_output_path(out, args.lineage_out)
        starts = tuple(args.start_ref or graph.default_start_refs())
        budgets = {
            "max_frontier_items": args.max_frontier_items,
            "max_depth": args.max_depth,
            "max_hypotheses": args.max_hypotheses,
            "max_investigation_rounds": args.max_investigation_rounds,
            "max_artifact_bytes": args.max_artifact_bytes,
            "max_judge_requests": args.max_judge_requests,
        }
        model_identity = stable_json(
            {
                "model": transport.model,
                "base_url": transport.base_url,
                "thinking": transport.thinking_config,
                "max_tokens": transport.max_tokens,
                "provider_error_threshold": transport.provider_error_threshold,
                "fusion_mode": args.fusion_mode,
            }
        )
        checkpoint_config = build_checkpoint_config(
            trace=graph.raw_trace,
            case_id=graph.case_id,
            objective=args.objective,
            analysis_perspective=args.analysis_perspective,
            start_refs=starts,
            budgets=budgets,
            model_identity=model_identity,
            cache_identity=str(cache_path.expanduser().resolve()),
            runtime_identity={
                "judge_timeout_sec": args.judge_timeout_sec,
                "judge_max_tokens": transport.max_tokens,
                "thinking_mode": stable_json(transport.thinking_config),
                "base_url": transport.base_url,
                "provider_error_threshold": transport.provider_error_threshold,
            },
        )
        causal_judge = ClaudeCausalJudge(
            transport=transport,
            cache=transport.cache
            if isinstance(transport.cache, JudgmentCache)
            else JudgmentCache(cache_path),
        )
        checkpoint = CheckpointBundle(checkpoint_path)
        with GracefulSignalState() as shutdown:
            report = AgenticRecursiveAnalyzer(
                judge=causal_judge,
                max_frontier_items=args.max_frontier_items,
                max_depth=args.max_depth,
                max_hypotheses=args.max_hypotheses,
                max_investigation_rounds=args.max_investigation_rounds,
                max_artifact_bytes=args.max_artifact_bytes,
                max_judge_requests=args.max_judge_requests,
                checkpoint=checkpoint,
                checkpoint_config=checkpoint_config,
                stop_requested=shutdown.stop_requested,
                fusion_mode=args.fusion_mode,
            ).analyze(
                graph,
                start_refs=starts,
                objective=args.objective,
                analysis_perspective=args.analysis_perspective,
            )
            report_payload = attribution_output_payload(report, graph)
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
                    report=report_payload, output_commit=output_commit
                )
    else:
        report = BackwardTaintAnalyzer(
            judge=transport, max_depth=args.max_depth, max_nodes=args.max_nodes
        ).analyze(
            graph,
            start_refs=args.start_ref or None,
            objective=args.objective,
        )
        atomic_write_json(out, attribution_output_payload(report, graph))
        lineage_out = lineage_output_path(out, args.lineage_out)
        atomic_write_json(lineage_out, graph.message_lineage)
    print(str(out))
    return 0


def attribution_output_payload(report: Any, graph: TraceGraph) -> dict[str, Any]:
    return annotate_report_semantic_anchors(
        graph.case_id, graph.nodes, report.to_dict(), graph=graph
    )


def lineage_output_path(attribution_out: Path, configured: str) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(f"{attribution_out.stem}.message-lineage.json")


def judge_cache_output_path(attribution_out: Path, configured: str) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(f"{attribution_out.stem}.judge-cache.jsonl")


def recursive_checkpoint_path(attribution_out: Path, configured: str) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(f"{attribution_out.stem}.checkpoint")


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a JSON output after fsyncing file and directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name), suffix=".tmp", dir=str(path.parent)
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
    trace_path: Path,
    review_path: Optional[Path] = None,
    evaluation_paths: Iterable[Path] = (),
) -> TraceGraph:
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if review_path:
        review = json.loads(review_path.read_text(encoding="utf-8"))
        trace = inject_quality_gap_records(trace, review)
    payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in evaluation_paths
    ]
    if payloads:
        trace = inject_external_evaluation_facts(trace, payloads)
    return TraceGraph.from_trace(
        trace,
        artifact_root=artifact_root_for_trace_path(trace_path, trace),
    )


if __name__ == "__main__":
    raise SystemExit(main())
