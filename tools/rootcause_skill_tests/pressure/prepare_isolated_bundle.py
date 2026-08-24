#!/usr/bin/env python3
"""Create a minimal, evaluator-free input bundle for one RED pressure run."""

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tools" / "rootcause_skill_tests" / "fixtures"
PROMPTS = {
    "known-root": Path(__file__).with_name("baseline-known-root.md"),
    "ambiguous": Path(__file__).with_name("baseline-ambiguous.md"),
}


def relative_artifact_path(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Artifact path must stay within the selected fixture: {value}")
    return path


def fixture_file(source, relative):
    root = source.resolve(strict=True)
    if source.is_symlink():
        raise ValueError(f"Selected fixture must not be a symlink: {source}")

    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"Fixture path must not traverse a symlink: {relative}")

    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Fixture path escapes the selected case: {relative}") from error
    if not resolved.is_file():
        raise ValueError(f"Referenced file is missing: {candidate}")
    return resolved


def prepare(case, destination):
    prompt_path = PROMPTS.get(case)
    if prompt_path is None:
        raise ValueError(f"No RED baseline prompt is defined for case: {case}")

    source = FIXTURES / case
    trace_source = fixture_file(source, Path("trace.json"))
    trace = json.loads(trace_source.read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=False)
    trace_destination = destination / "trace.json"
    shutil.copyfile(trace_source, trace_destination)

    for artifact in trace["artifacts"]:
        relative = relative_artifact_path(artifact["path"])
        artifact_source = fixture_file(source, relative)
        artifact_destination = destination / relative
        artifact_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact_source, artifact_destination)

    prompt = prompt_path.read_text(encoding="utf-8").replace("{{TRACE_PATH}}", str(trace_destination))
    (destination / "prompt.md").write_text(prompt, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(PROMPTS), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepare(args.case, args.destination)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
