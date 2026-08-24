#!/usr/bin/env python3
"""Run root-cause pressure cases through an external OpenCode binary in isolated workspaces."""

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
BUILDER = REPO / "tools" / "rootcause_skill_tests" / "pressure" / "prepare_isolated_bundle.py"
CASE_NAMES = (
    "known-root",
    "ambiguous",
    "skill-omission",
    "context-contamination",
    "control-flow-change",
    "tool-failure-misreported",
    "wrong-answer",
)
DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_SIGNAL_GRACE_SECONDS = 3.0


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def positive_float(value):
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def unique_default_batch_root():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path("/tmp/rootcause-forward-attempts") / (
        f"run-{timestamp}-pid{os.getpid()}-{uuid.uuid4().hex}"
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-root",
        default=os.environ.get("ROOTCAUSE_FORWARD_BATCH_ROOT"),
        help="fresh output directory; must not already exist",
    )
    parser.add_argument(
        "--opencode-bin",
        default=os.environ.get("OPENCODE_BIN"),
        help="standalone OpenCode executable; defaults to OPENCODE_BIN",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=CASE_NAMES,
        help="run only the named case; repeat to select more than one",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--signal-grace-seconds",
        type=positive_float,
        default=DEFAULT_SIGNAL_GRACE_SECONDS,
    )
    args = parser.parse_args()
    args.batch_root = Path(args.batch_root) if args.batch_root else unique_default_batch_root()
    return args


def resolve_executable(value):
    if not value:
        raise ValueError("Set OPENCODE_BIN or pass --opencode-bin for a scored run")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        discovered = shutil.which(value)
        if discovered is None:
            raise ValueError(f"OpenCode executable was not found: {value}")
        candidate = Path(discovered)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
        raise ValueError(f"OpenCode executable is not executable: {resolved}")
    return resolved


def prepare_workspace(fixture, workspace):
    command = [
        sys.executable,
        str(BUILDER),
        "--case",
        fixture,
        "--destination",
        str(workspace),
    ]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown builder failure"
        raise RuntimeError(f"isolated bundle preparation failed: {detail}")
    return {
        "command_argv": command,
        "raw_returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def case_context(batch_root, fixture, opencode_bin, timeout_seconds, signal_grace_seconds):
    case_root = batch_root / fixture
    workspace = case_root / "workspace"
    runtime_root = case_root / "runtime"
    builder_result = prepare_workspace(fixture, workspace)
    prompt_path = workspace / "prompt-opencode.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    executable = resolve_executable(opencode_bin)
    argv = [
        str(executable),
        "run",
        "--print-logs",
        "--log-level",
        "ERROR",
        "--format",
        "json",
        prompt,
    ]
    env_overrides = {
        "HOME": str(runtime_root / "home"),
        "PWD": str(workspace),
        "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "XDG_DATA_HOME": str(runtime_root / "xdg" / "data"),
        "XDG_CACHE_HOME": str(runtime_root / "xdg" / "cache"),
        "XDG_CONFIG_HOME": str(runtime_root / "xdg" / "config"),
        "XDG_STATE_HOME": str(runtime_root / "xdg" / "state"),
    }
    return {
        "case": fixture,
        "case_root": case_root,
        "workspace": workspace,
        "trace_path": workspace / "trace.json",
        "runtime_root": runtime_root,
        "timeout_seconds": timeout_seconds,
        "signal_grace_seconds": signal_grace_seconds,
        "prompt_path": prompt_path,
        "prompt": prompt,
        "command_argv": argv,
        "command_display": shlex.join(argv),
        "environment_overrides": env_overrides,
        "builder_result": builder_result,
    }


def safe_signal(process, requested_signal, audit):
    signal_name = signal.Signals(requested_signal).name
    audit["signal_attempted"].append(signal_name)
    if process.poll() is not None:
        audit["signal_events"].append(
            {"signal": signal_name, "sent": False, "reason": "process_already_exited"}
        )
        return False
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        audit["signal_events"].append(
            {"signal": signal_name, "sent": False, "reason": "process_not_found"}
        )
        return False
    except PermissionError as error:
        audit["signal_events"].append(
            {
                "signal": signal_name,
                "sent": False,
                "reason": "permission_denied",
                "error": str(error),
            }
        )
        return False
    audit["signal_sent"].append(signal_name)
    audit["last_signal_sent"] = signal_name
    audit["signal_events"].append({"signal": signal_name, "sent": True, "reason": None})
    return True


def text_output(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def communicate_with_grace(process, grace_seconds):
    try:
        stdout, stderr = process.communicate(timeout=grace_seconds)
        return True, text_output(stdout), text_output(stderr)
    except subprocess.TimeoutExpired as error:
        return False, text_output(error.stdout), text_output(error.stderr)


def run_case(batch_root, fixture, args):
    context = case_context(
        batch_root,
        fixture,
        args.opencode_bin,
        args.timeout_seconds,
        args.signal_grace_seconds,
    )
    env = os.environ.copy()
    env.pop("OLDPWD", None)
    env.pop("INIT_CWD", None)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.update(context["environment_overrides"])

    started_at = utc_now()
    started_monotonic = time.monotonic()
    process = None
    stdout = ""
    stderr = ""
    raw_returncode = None
    timed_out = False
    wrapper_error = None
    audit = {
        "signal_attempted": [],
        "signal_sent": [],
        "last_signal_sent": None,
        "signal_events": [],
        "post_signal_returncode": None,
        "final_cleanup": {
            "process_reaped": False,
            "process_running_at_end": None,
            "note": "not_started",
        },
    }

    try:
        process = subprocess.Popen(
            context["command_argv"],
            cwd=context["workspace"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=args.timeout_seconds)
            raw_returncode = process.returncode
            audit["final_cleanup"] = {
                "process_reaped": True,
                "process_running_at_end": False,
                "note": "process_exited_before_timeout",
            }
        except subprocess.TimeoutExpired:
            timed_out = True
            for requested_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                safe_signal(process, requested_signal, audit)
                reaped, stdout, stderr = communicate_with_grace(
                    process, args.signal_grace_seconds
                )
                if reaped:
                    break
            audit["post_signal_returncode"] = process.poll()
            still_running = process.poll() is None
            audit["final_cleanup"] = {
                "process_reaped": not still_running,
                "process_running_at_end": still_running,
                "note": (
                    "process reaped after one or more signal attempts; no signal is "
                    "asserted as the cause"
                    if not still_running
                    else "process still running after all bounded signal attempts"
                ),
            }
    except Exception as error:
        wrapper_error = {"type": type(error).__name__, "message": str(error)}
    finally:
        if process is not None and process.poll() is None:
            safe_signal(process, signal.SIGKILL, audit)
            reaped, cleanup_stdout, cleanup_stderr = communicate_with_grace(
                process, args.signal_grace_seconds
            )
            stdout = cleanup_stdout or stdout
            stderr = cleanup_stderr or stderr
            audit["post_signal_returncode"] = process.poll()
            audit["final_cleanup"] = {
                "process_reaped": reaped,
                "process_running_at_end": process.poll() is None,
                "note": (
                    "final cleanup reaped process after a signal attempt; no signal is "
                    "asserted as the cause"
                    if reaped
                    else "final bounded cleanup could not reap process"
                ),
            }

    return {
        "run_kind": "scored_isolated_forward",
        "evaluation_status": "unscored_pending_evaluator",
        "case": fixture,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        "cwd": str(context["workspace"]),
        "batch_root": str(batch_root),
        "workspace": str(context["workspace"]),
        "trace_path": str(context["trace_path"]),
        "prompt_path": str(context["prompt_path"]),
        "timeout_seconds": args.timeout_seconds,
        "signal_grace_seconds": args.signal_grace_seconds,
        "command_argv": context["command_argv"],
        "command_display": context["command_display"],
        "prompt": context["prompt"],
        "environment_overrides": context["environment_overrides"],
        "builder_result": context["builder_result"],
        "stdout": text_output(stdout),
        "stderr": text_output(stderr),
        "raw_returncode": raw_returncode,
        "timed_out": timed_out,
        "signal_attempted": audit["signal_attempted"],
        "signal_sent": audit["signal_sent"],
        "last_signal_sent": audit["last_signal_sent"],
        "signal_events": audit["signal_events"],
        "post_signal_returncode": audit["post_signal_returncode"],
        "final_cleanup": audit["final_cleanup"],
        "wrapper_error": wrapper_error,
    }


def wrapper_failure_record(batch_root, fixture, args, error):
    case_root = batch_root / fixture
    workspace = case_root / "workspace"
    observed_at = utc_now()
    return {
        "run_kind": "scored_isolated_forward",
        "evaluation_status": "unscored_wrapper_error",
        "case": fixture,
        "started_at": observed_at,
        "finished_at": observed_at,
        "elapsed_seconds": 0.0,
        "cwd": str(workspace),
        "batch_root": str(batch_root),
        "workspace": str(workspace),
        "trace_path": str(workspace / "trace.json"),
        "prompt_path": str(workspace / "prompt-opencode.md"),
        "timeout_seconds": args.timeout_seconds,
        "signal_grace_seconds": args.signal_grace_seconds,
        "command_argv": [],
        "command_display": "",
        "prompt": "",
        "environment_overrides": {},
        "builder_result": None,
        "stdout": "",
        "stderr": "",
        "raw_returncode": None,
        "timed_out": False,
        "signal_attempted": [],
        "signal_sent": [],
        "last_signal_sent": None,
        "signal_events": [],
        "post_signal_returncode": None,
        "final_cleanup": {
            "process_reaped": False,
            "process_running_at_end": None,
            "note": "case wrapper failed before process state was established",
        },
        "wrapper_error": {"type": type(error).__name__, "message": str(error)},
    }


def write_result(stream, result):
    line = json.dumps(result, ensure_ascii=False, sort_keys=True)
    stream.write(line + "\n")
    stream.flush()
    os.fsync(stream.fileno())
    print(line, flush=True)


def main():
    args = parse_args()
    batch_root = args.batch_root.resolve(strict=False)
    batch_root.mkdir(parents=True, exist_ok=False)
    selected_names = set(args.case or [])
    selected_cases = [case for case in CASE_NAMES if not selected_names or case in selected_names]
    results_path = batch_root / "results.jsonl"
    with results_path.open("x", encoding="utf-8") as stream:
        for fixture in selected_cases:
            try:
                result = run_case(batch_root, fixture, args)
            except Exception as error:
                result = wrapper_failure_record(batch_root, fixture, args, error)
                write_result(stream, result)
                continue
            write_result(stream, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
