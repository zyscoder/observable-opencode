#!/usr/bin/env python3
"""Run root-cause pressure cases in explicit smoke or sandboxed scored mode."""

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
DEFAULT_CONTAINER_IMAGE = "python:3.12-slim-bookworm"
SANITIZED_PROVIDER_ENV = (
    "MODEL",
    "URL",
    "APIKEY",
    "OPENCODE_CONFIG_CONTENT",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
)
SENSITIVE_PROVIDER_ENV = (
    "URL",
    "APIKEY",
    "OPENCODE_CONFIG_CONTENT",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
)
CONTAINER_CLIENT_ENV = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "CONTAINER_HOST",
)


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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "scored"), required=True)
    parser.add_argument(
        "--batch-root",
        default=os.environ.get("ROOTCAUSE_FORWARD_BATCH_ROOT"),
        help="fresh evaluator-side audit directory; must not already exist",
    )
    parser.add_argument(
        "--opencode-bin",
        default=os.environ.get("OPENCODE_BIN"),
        help="external executable; scored mode requires a Linux release ELF",
    )
    parser.add_argument(
        "--container-runtime",
        choices=("docker", "podman"),
        help="required filesystem sandbox for scored mode",
    )
    parser.add_argument("--container-image", default=DEFAULT_CONTAINER_IMAGE)
    parser.add_argument("--case", action="append", choices=CASE_NAMES)
    parser.add_argument("--timeout-seconds", type=positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--signal-grace-seconds",
        type=positive_float,
        default=DEFAULT_SIGNAL_GRACE_SECONDS,
    )
    args = parser.parse_args(argv)
    args.batch_root = Path(args.batch_root) if args.batch_root else unique_default_batch_root()
    return args


def resolve_executable(value):
    if not value:
        raise ValueError("Set OPENCODE_BIN or pass --opencode-bin")
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


def validate_linux_release_binary(path):
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic != b"\x7fELF":
        raise ValueError(
            "Scored mode requires an external Linux release OPENCODE_BIN (ELF), "
            "not a source build or host-native executable"
        )


def resolve_container_runtime(value):
    if not value:
        raise ValueError(
            "Scored mode requires --container-runtime docker or podman as a real "
            "filesystem sandbox"
        )
    executable = shutil.which(value)
    if executable is None:
        raise ValueError(f"Scored filesystem sandbox runtime is unavailable: {value}")
    result = subprocess.run(
        [executable, "info"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        env={key: os.environ[key] for key in CONTAINER_CLIENT_ENV if key in os.environ},
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "runtime info failed"
        raise ValueError(f"Scored filesystem sandbox runtime is unavailable: {detail}")
    return executable


def provider_environment(source=None):
    source = os.environ if source is None else source
    return {key: source[key] for key in SANITIZED_PROVIDER_ENV if key in source}


def redact_provider_values(value, provider_env):
    redacted = text_output(value)
    secrets = (
        (name, provider_env.get(name))
        for name in SENSITIVE_PROVIDER_ENV
        if provider_env.get(name)
    )
    for name, secret in sorted(secrets, key=lambda item: len(item[1]), reverse=True):
        redacted = redacted.replace(secret, f"<redacted:{name}>")
    return redacted


def subprocess_environment(provider_env):
    names = set(CONTAINER_CLIENT_ENV) | set(SANITIZED_PROVIDER_ENV)
    return {key: value for key, value in os.environ.items() if key in names} | provider_env


def mount_argument(source, destination):
    return f"type=bind,src={source.resolve()},dst={destination},readonly"


def build_scored_container_command(
    runtime,
    image,
    workspace,
    opencode_bin,
    prompt,
    provider_env,
):
    command = [
        str(runtime),
        "run",
        "--rm",
        "--read-only",
        "--workdir",
        "/workspace",
        "--mount",
        mount_argument(workspace, "/workspace"),
        "--mount",
        mount_argument(opencode_bin, "/opt/opencode"),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
    ]
    for value in (
        "HOME=/tmp/home",
        "XDG_DATA_HOME=/tmp/xdg/data",
        "XDG_CACHE_HOME=/tmp/xdg/cache",
        "XDG_CONFIG_HOME=/tmp/xdg/config",
        "XDG_STATE_HOME=/tmp/xdg/state",
        "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1",
        "OPENCODE_DISABLE_MODELS_FETCH=1",
    ):
        command.extend(("--env", value))
    for name in SANITIZED_PROVIDER_ENV:
        if name in provider_env:
            command.extend(("--env", name))
    command.extend(
        (
            image,
            "/opt/opencode",
            "run",
            "--print-logs",
            "--log-level",
            "ERROR",
            "--format",
            "json",
            prompt,
        )
    )
    return command


def build_scored_probe_command(runtime, image, workspace, opencode_bin, canary_path):
    script = (
        "test -f /workspace/trace.json; "
        "test ! -e /workspace/../evaluator; "
        "test ! -e /workspace/../audit; "
        "test ! -e /workspace/../batch; "
        f"test ! -e {shlex.quote(str(canary_path))}"
    )
    return [
        str(runtime),
        "run",
        "--rm",
        "--read-only",
        "--workdir",
        "/workspace",
        "--mount",
        mount_argument(workspace, "/workspace"),
        "--mount",
        mount_argument(opencode_bin, "/opt/opencode"),
        image,
        "sh",
        "-eu",
        "-c",
        script,
    ]


def container_mounts(command):
    mounts = {}
    for index, value in enumerate(command[:-1]):
        if value != "--mount":
            continue
        fields = dict(item.split("=", 1) for item in command[index + 1].split(",") if "=" in item)
        mounts[fields["src"]] = fields["dst"]
    return mounts


def prepare_workspace(fixture, workspace, opaque_case_id, agent_trace_path):
    command = [
        sys.executable,
        str(BUILDER),
        "--case",
        fixture,
        "--destination",
        str(workspace),
        "--opaque-case-id",
        opaque_case_id,
        "--agent-trace-path",
        agent_trace_path,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown builder failure"
        raise RuntimeError(f"isolated bundle preparation failed: {detail}")
    return json.loads(result.stdout)


def case_context(batch_root, fixture, args, executable, container_runtime=None):
    opaque_case_id = f"case-{uuid.uuid4().hex}"
    workspace = batch_root / "workspaces" / opaque_case_id
    runtime_root = batch_root / "smoke-runtime" / opaque_case_id
    agent_trace_path = "/workspace/trace.json" if args.mode == "scored" else str(workspace / "trace.json")
    provenance = prepare_workspace(fixture, workspace, opaque_case_id, agent_trace_path)
    prompt_path = workspace / "prompt-opencode.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    provider_env = provider_environment()
    if args.mode == "scored":
        argv = build_scored_container_command(
            container_runtime,
            args.container_image,
            workspace,
            executable,
            prompt,
            provider_env,
        )
        host_cwd = batch_root
        environment = subprocess_environment(provider_env)
    else:
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
        host_cwd = workspace
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(runtime_root / "home"),
                "PWD": str(workspace),
                "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1",
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "XDG_DATA_HOME": str(runtime_root / "xdg/data"),
                "XDG_CACHE_HOME": str(runtime_root / "xdg/cache"),
                "XDG_CONFIG_HOME": str(runtime_root / "xdg/config"),
                "XDG_STATE_HOME": str(runtime_root / "xdg/state"),
            }
        )
    return {
        "fixture": fixture,
        "opaque_case_id": opaque_case_id,
        "workspace": workspace,
        "trace_path": workspace / "trace.json",
        "runtime_root": runtime_root,
        "prompt_path": prompt_path,
        "prompt": prompt,
        "command_argv": argv,
        "command_display": shlex.join(argv),
        "host_cwd": host_cwd,
        "environment": environment,
        "provider_environment": provider_env,
        "provider_environment_names": sorted(provider_env),
        "derived_provenance": provenance,
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
            {"signal": signal_name, "sent": False, "reason": "permission_denied", "error": str(error)}
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


def run_process(context, args):
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
        "final_cleanup": {"process_reaped": False, "process_running_at_end": None, "note": "not_started"},
    }
    try:
        process = subprocess.Popen(
            context["command_argv"],
            cwd=context["host_cwd"],
            env=context["environment"],
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
                reaped, stdout, stderr = communicate_with_grace(process, args.signal_grace_seconds)
                if reaped:
                    break
            audit["post_signal_returncode"] = process.poll()
            still_running = process.poll() is None
            audit["final_cleanup"] = {
                "process_reaped": not still_running,
                "process_running_at_end": still_running,
                "note": (
                    "process reaped after one or more signal attempts; no signal is asserted as the cause"
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
                "note": "final cleanup completed; no requested signal is asserted as termination cause",
            }
    return stdout, stderr, raw_returncode, timed_out, wrapper_error, audit


def run_case(batch_root, fixture, args, executable, container_runtime=None):
    context = case_context(batch_root, fixture, args, executable, container_runtime)
    probe = None
    if args.mode == "scored":
        canary = batch_root / "evaluator-canary"
        canary.write_text("must remain host-side", encoding="utf-8")
        probe_command = build_scored_probe_command(
            container_runtime,
            args.container_image,
            context["workspace"],
            executable,
            canary,
        )
        probe_result = subprocess.run(
            probe_command,
            cwd=batch_root,
            env=context["environment"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        probe = {
            "command_argv": probe_command,
            "raw_returncode": probe_result.returncode,
            "stdout": redact_provider_values(probe_result.stdout, context["provider_environment"]),
            "stderr": redact_provider_values(probe_result.stderr, context["provider_environment"]),
            "asserted_unavailable": ["workspace_parent", "evaluator", "audit", "host_canary"],
        }
        if probe_result.returncode != 0:
            raise RuntimeError("scored filesystem isolation adversarial probe failed")

    started_at = utc_now()
    started_monotonic = time.monotonic()
    stdout, stderr, raw_returncode, timed_out, wrapper_error, audit = run_process(context, args)
    run_kind = "scored_container" if args.mode == "scored" else "smoke_local"
    evaluation_status = "scored_pending_evaluator" if args.mode == "scored" else "unscored_smoke"
    return {
        "run_kind": run_kind,
        "evaluation_status": evaluation_status,
        "case": fixture,
        "opaque_case_id": context["opaque_case_id"],
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        "cwd": str(context["host_cwd"]),
        "batch_root": str(batch_root),
        "workspace": str(context["workspace"]),
        "trace_path": str(context["trace_path"]),
        "prompt_path": str(context["prompt_path"]),
        "timeout_seconds": args.timeout_seconds,
        "signal_grace_seconds": args.signal_grace_seconds,
        "command_argv": context["command_argv"],
        "command_display": context["command_display"],
        "prompt": context["prompt"],
        "provider_environment_names": context["provider_environment_names"],
        "derived_provenance": context["derived_provenance"],
        "container_probe": probe,
        "stdout": redact_provider_values(stdout, context["provider_environment"]),
        "stderr": redact_provider_values(stderr, context["provider_environment"]),
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
    observed_at = utc_now()
    return {
        "run_kind": "scored_container" if args.mode == "scored" else "smoke_local",
        "evaluation_status": "unscored_wrapper_error",
        "case": fixture,
        "started_at": observed_at,
        "finished_at": observed_at,
        "elapsed_seconds": 0.0,
        "batch_root": str(batch_root),
        "raw_returncode": None,
        "timed_out": False,
        "signal_attempted": [],
        "signal_sent": [],
        "last_signal_sent": None,
        "post_signal_returncode": None,
        "final_cleanup": {"process_reaped": False, "process_running_at_end": None, "note": "not_started"},
        "wrapper_error": {"type": type(error).__name__, "message": str(error)},
    }


def write_result(stream, result):
    line = json.dumps(result, ensure_ascii=False, sort_keys=True)
    stream.write(line + "\n")
    stream.flush()
    os.fsync(stream.fileno())
    print(line, flush=True)


def main(argv=None):
    args = parse_args(argv)
    try:
        executable = resolve_executable(args.opencode_bin)
        container_runtime = None
        if args.mode == "scored":
            validate_linux_release_binary(executable)
            container_runtime = resolve_container_runtime(args.container_runtime)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    batch_root = args.batch_root.resolve(strict=False)
    batch_root.mkdir(parents=True, exist_ok=False)
    selected_names = set(args.case or [])
    selected_cases = [case for case in CASE_NAMES if not selected_names or case in selected_names]
    results_path = batch_root / "results.jsonl"
    with results_path.open("x", encoding="utf-8") as stream:
        for fixture in selected_cases:
            try:
                result = run_case(batch_root, fixture, args, executable, container_runtime)
            except Exception as error:
                result = wrapper_failure_record(batch_root, fixture, args, error)
                write_result(stream, result)
                continue
            write_result(stream, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
