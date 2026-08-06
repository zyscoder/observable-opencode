from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from .claude import default_judge_timeout_seconds
from .request import AttributionOptions, AttributionRequest
from .errors import AttributionInputError
from .service import (
    GracefulSignalState,
    analysis_start_refs,
    analyze,
    attribution_output_payload,
    atomic_write_json,
    judge_cache_output_path,
    lineage_output_path,
    load_graph,
    recursive_checkpoint_path,
)


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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace", help="Path to observable-opencode trace.json")
    source.add_argument(
        "--benchmark-bundle",
        default="",
        help="Path to a verified immutable benchmark Trace bundle",
    )
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
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--question", default="")
    target.add_argument("--objective", default="")
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
    try:
        request = AttributionRequest(
            trace_path=Path(args.trace) if args.trace else None,
            benchmark_bundle_path=(
                Path(args.benchmark_bundle) if args.benchmark_bundle else None
            ),
            output_path=Path(args.out),
            review_path=Path(args.review) if args.review else None,
            evaluation_paths=tuple(Path(path) for path in args.evaluation),
            start_refs=tuple(args.start_ref),
            question=args.question,
            objective=args.objective,
            options=AttributionOptions(
                engine=args.engine,
                fusion_mode=args.fusion_mode,
                analysis_perspective=args.analysis_perspective,
                max_depth=args.max_depth,
                max_nodes=args.max_nodes,
                max_frontier_items=args.max_frontier_items,
                max_hypotheses=args.max_hypotheses,
                max_investigation_rounds=args.max_investigation_rounds,
                max_artifact_bytes=args.max_artifact_bytes,
                max_judge_requests=args.max_judge_requests,
                lineage_output_path=(Path(args.lineage_out) if args.lineage_out else None),
                judge_cache_path=(Path(args.judge_cache) if args.judge_cache else None),
                checkpoint_path=(Path(args.checkpoint_dir) if args.checkpoint_dir else None),
                model=args.model,
                api_key_env=args.api_key_env,
                base_url=args.base_url,
                base_url_env=args.base_url_env,
                judge_timeout_sec=args.judge_timeout_sec,
                judge_max_tokens=args.judge_max_tokens,
                thinking_mode=args.thinking_mode,
                provider_error_threshold=args.provider_error_threshold,
            ),
        )
    except ValueError as exc:
        raise SystemExit("error: {0}".format(exc)) from exc
    try:
        analyze(request)
    except AttributionInputError as exc:
        raise SystemExit("error: {0}".format(exc)) from exc
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
