from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from .analyzer import BackwardTaintAnalyzer
from .claude import ClaudeJudgeClient
from .graph import TraceGraph
from .quality_review import inject_quality_gap_records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run backward semantic taint attribution on trace.json.")
    parser.add_argument("--trace", required=True, help="Path to observable-opencode trace.json")
    parser.add_argument("--review", default="", help="Optional trace-review JSON; quality gaps are injected as start nodes")
    parser.add_argument("--out", required=True, help="Path to write attribution JSON report")
    parser.add_argument("--objective", default="Find the root cause of the observed bad final result.")
    parser.add_argument("--start-ref", action="append", default=[], help="Trace ref to start from; repeatable")
    parser.add_argument("--model", default="", help="Claude model id; defaults to CLAUDE_MODEL or claude-sonnet-4-5")
    parser.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    parser.add_argument("--base-url", default="", help="Anthropic-compatible API base URL; defaults to ANTHROPIC_BASE_URL")
    parser.add_argument("--base-url-env", default="ANTHROPIC_BASE_URL")
    parser.add_argument(
        "--judge-max-tokens",
        type=int,
        default=4096,
        help="Max output tokens for each judge call; reasoning models may need extra room for JSON text.",
    )
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-nodes", type=int, default=48)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    graph = load_graph(Path(args.trace), Path(args.review) if args.review else None)
    judge = ClaudeJudgeClient(
        model=args.model,
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        base_url_env=args.base_url_env,
        max_tokens=args.judge_max_tokens,
    )
    report = BackwardTaintAnalyzer(judge=judge, max_depth=args.max_depth, max_nodes=args.max_nodes).analyze(
        graph,
        start_refs=args.start_ref or None,
        objective=args.objective,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out))
    return 0


def load_graph(trace_path: Path, review_path: Optional[Path] = None) -> TraceGraph:
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if review_path:
        review = json.loads(review_path.read_text(encoding="utf-8"))
        trace = inject_quality_gap_records(trace, review)
    return TraceGraph.from_trace(trace)


if __name__ == "__main__":
    raise SystemExit(main())
