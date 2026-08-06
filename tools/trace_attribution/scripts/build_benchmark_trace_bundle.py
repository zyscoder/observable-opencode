#!/usr/bin/env python3
"""Build one immutable benchmark Trace bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trace_attribution.benchmark_bundle import build_benchmark_trace_bundle


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--review")
    parser.add_argument("--evaluation", action="append", default=[])
    parser.add_argument("--labels")
    parser.add_argument("--store", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        bundle = build_benchmark_trace_bundle(
            trace_path=Path(args.trace),
            store_dir=Path(args.store),
            artifact_root=Path(args.artifact_root),
            review_path=Path(args.review) if args.review else None,
            evaluation_paths=tuple(Path(value) for value in args.evaluation),
            labels_path=Path(args.labels) if args.labels else None,
        )
    except (OSError, TypeError, ValueError) as error:
        print("error: {0}".format(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "bundle_id": bundle.bundle_id,
                "bundle_path": str(bundle.root),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
