#!/usr/bin/env python3
"""Run root-cause pressure cases in explicit smoke or sandboxed scored mode."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.parse
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
SENSITIVE_CONFIG_KEYS = (
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
    "credential",
    "credentials",
    "accesskey",
    "privatekey",
    "clientsecret",
    "bearer",
    "auth",
)
PINNED_IMAGE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
VERSION_PATTERN = re.compile(
    r"(?i)(?:(?:observable-)?opencode[^\r\n]{0,80})?\bv?\d+\.\d+(?:\.\d+)?\b"
)
ELF_ARCHITECTURES = {
    62: ("x86_64", "linux/amd64"),
    183: ("aarch64", "linux/arm64"),
}
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
    parser.add_argument(
        "--container-image",
        help="digest-pinned image reference name@sha256:<64 lowercase hex>",
    )
    parser.add_argument(
        "--opencode-sha256",
        help="expected lowercase SHA-256 for the external Linux release binary",
    )
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


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_linux_release_binary(path, expected_sha256):
    if not expected_sha256 or not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("Scored mode requires --opencode-sha256 as 64 lowercase hex characters")
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError("OpenCode release SHA-256 does not match --opencode-sha256")
    with path.open("rb") as stream:
        header = stream.read(64)
    if len(header) < 64 or header[:4] != b"\x7fELF":
        raise ValueError("Scored OPENCODE_BIN must have a complete ELF64 header")
    if header[4] != 2 or header[5] != 1 or header[6] != 1:
        raise ValueError("Scored OPENCODE_BIN must be ELF64 little-endian version 1")
    if header[7] not in (0, 3):
        raise ValueError("Scored OPENCODE_BIN must declare the System V or Linux ELF ABI")
    elf_type, machine, elf_version = struct.unpack_from("<HHI", header, 16)
    if elf_type not in (2, 3) or elf_version != 1:
        raise ValueError("Scored OPENCODE_BIN must be an executable Linux ELF64 release")
    architecture = ELF_ARCHITECTURES.get(machine)
    if architecture is None:
        raise ValueError("Scored OPENCODE_BIN architecture must be x86_64 or aarch64")
    return {
        "sha256": actual_sha256,
        "architecture": architecture[0],
        "platform": architecture[1],
    }


def validate_container_image(value):
    if not value:
        raise ValueError("Scored mode requires --container-image pinned by digest")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Container image contains a control character")
    if value.startswith("-") or not PINNED_IMAGE_PATTERN.fullmatch(value):
        raise ValueError(
            "Container image must be an explicit name@sha256:<64 lowercase hex> reference"
        )
    return value


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


def normalized_secret_key(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sensitive_config_key(value):
    normalized = normalized_secret_key(value)
    return any(
        normalized == marker or normalized.endswith(marker) or marker in normalized
        for marker in SENSITIVE_CONFIG_KEYS
    )


def collect_sensitive_config_values(value, path="config", inherited_sensitive=False):
    collected = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            collected.extend(
                collect_sensitive_config_values(
                    child,
                    child_path,
                    inherited_sensitive or sensitive_config_key(key_text),
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            collected.extend(
                collect_sensitive_config_values(
                    child,
                    f"{path}[{index}]",
                    inherited_sensitive,
                )
            )
    elif inherited_sensitive and isinstance(value, (str, int, float, bool)):
        collected.append((path, str(value)))
    return collected


def secret_variants(value):
    if not isinstance(value, str) or not value:
        return set()
    serialized = {
        value,
        json.dumps(value, ensure_ascii=False),
        json.dumps(value, ensure_ascii=True),
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value, ensure_ascii=True)[1:-1],
        value.replace("\\", "\\\\").replace('"', '\\"'),
        value.replace("/", "\\/"),
        repr(value),
    }
    serialized.update(item.replace("/", "\\/") for item in tuple(serialized))
    variants = set(serialized)
    for item in serialized:
        variants.update(
            {
                urllib.parse.quote(item, safe=""),
                urllib.parse.quote_plus(item, safe=""),
                urllib.parse.quote(item, safe="").lower(),
                urllib.parse.quote_plus(item, safe="").lower(),
            }
        )
    return {variant for variant in variants if variant}


def build_redaction_materials(provider_env, scored=False):
    materials = {}

    def add(label, secret):
        for variant in secret_variants(secret):
            previous = materials.get(variant)
            if previous is None or len(label) < len(previous):
                materials[variant] = label

    for name in SENSITIVE_PROVIDER_ENV:
        if provider_env.get(name):
            add(name, provider_env[name])

    config = provider_env.get("OPENCODE_CONFIG_CONTENT")
    if config:
        add("OPENCODE_CONFIG_CONTENT", config)
        try:
            parsed = json.loads(config)
        except json.JSONDecodeError as error:
            if scored:
                raise ValueError(
                    "OPENCODE_CONFIG_CONTENT must be valid JSON in scored mode"
                ) from error
        else:
            for path, secret in collect_sensitive_config_values(parsed):
                add(path, secret)
    return materials


def redact_sensitive_values(value, materials):
    redacted = text_output(value)
    for variant, label in sorted(
        materials.items(), key=lambda item: len(item[0]), reverse=True
    ):
        redacted = redacted.replace(variant, f"<redacted:{label}>")
    return redacted


def subprocess_environment(provider_env):
    names = set(CONTAINER_CLIENT_ENV) | set(SANITIZED_PROVIDER_ENV)
    return {key: value for key, value in os.environ.items() if key in names} | provider_env


def validate_mount_source(source, expected_kind):
    raw = os.fspath(source)
    if not raw or any(
        character == "," or ord(character) < 32 or ord(character) == 127
        for character in raw
    ):
        raise ValueError("Mount source contains a comma or control character")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("Mount source must be an absolute canonical path")
    resolved = candidate.resolve(strict=True)
    if candidate != resolved or candidate.is_symlink():
        raise ValueError("Mount source must be a strict resolved path without symlinks")
    if expected_kind == "directory" and not resolved.is_dir():
        raise ValueError("Workspace mount source must be a directory")
    if expected_kind == "file" and not resolved.is_file():
        raise ValueError("Binary mount source must be a regular file")
    if expected_kind not in ("directory", "file"):
        raise ValueError(f"Unsupported mount source kind: {expected_kind}")
    return resolved


def mount_argument(source, destination, expected_kind):
    if destination not in ("/workspace", "/opt/opencode"):
        raise ValueError("Container mount destination is not allowed")
    resolved = validate_mount_source(source, expected_kind)
    return f"type=bind,src={resolved},dst={destination},readonly"


def validate_platform(value):
    if value not in {item[1] for item in ELF_ARCHITECTURES.values()}:
        raise ValueError("Container platform must match the validated Linux ELF architecture")
    return value


def build_scored_container_command(
    runtime,
    image,
    workspace,
    opencode_bin,
    prompt,
    provider_env,
    platform,
):
    image = validate_container_image(image)
    platform = validate_platform(platform)
    command = [
        str(runtime),
        "run",
        "--rm",
        "--read-only",
        "--platform",
        platform,
        "--workdir",
        "/workspace",
        "--mount",
        mount_argument(workspace, "/workspace", "directory"),
        "--mount",
        mount_argument(opencode_bin, "/opt/opencode", "file"),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--entrypoint",
        "/opt/opencode",
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


def build_scored_probe_command(
    runtime, image, workspace, opencode_bin, canary_path, platform
):
    image = validate_container_image(image)
    platform = validate_platform(platform)
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
        "--platform",
        platform,
        "--workdir",
        "/workspace",
        "--mount",
        mount_argument(workspace, "/workspace", "directory"),
        "--mount",
        mount_argument(opencode_bin, "/opt/opencode", "file"),
        "--entrypoint",
        "/bin/sh",
        image,
        "-eu",
        "-c",
        script,
    ]


def build_scored_preflight_command(runtime, image, opencode_bin, platform):
    image = validate_container_image(image)
    platform = validate_platform(platform)
    return [
        str(runtime),
        "run",
        "--rm",
        "--read-only",
        "--platform",
        platform,
        "--mount",
        mount_argument(opencode_bin, "/opt/opencode", "file"),
        "--entrypoint",
        "/opt/opencode",
        image,
        "--version",
    ]


def container_client_environment():
    return {key: os.environ[key] for key in CONTAINER_CLIENT_ENV if key in os.environ}


def run_scored_preflight(runtime, image, opencode_bin, release):
    if validate_linux_release_binary(opencode_bin, release["sha256"]) != release:
        raise ValueError("OpenCode release identity changed before container preflight")
    command = build_scored_preflight_command(
        runtime, image, opencode_bin, release["platform"]
    )
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=container_client_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("OpenCode container preflight did not self-exit within 30 seconds") from error
    if result.returncode != 0:
        raise ValueError("OpenCode container preflight must self-exit 0")
    observed = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    match = VERSION_PATTERN.search(observed)
    if match is None:
        raise ValueError("OpenCode container preflight did not emit plausible version output")
    return {
        "release_sha256": release["sha256"],
        "release_architecture": release["architecture"],
        "platform": release["platform"],
        "container_image": image,
        "version": match.group(0).strip(),
        "raw_returncode": result.returncode,
    }


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


def case_context(
    batch_root,
    fixture,
    args,
    executable,
    provider_env,
    redaction_materials,
    release=None,
    container_runtime=None,
):
    opaque_case_id = f"case-{uuid.uuid4().hex}"
    workspace = batch_root / "workspaces" / opaque_case_id
    runtime_root = batch_root / "smoke-runtime" / opaque_case_id
    agent_trace_path = "/workspace/trace.json" if args.mode == "scored" else str(workspace / "trace.json")
    provenance = prepare_workspace(fixture, workspace, opaque_case_id, agent_trace_path)
    prompt_path = workspace / "prompt-opencode.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    if args.mode == "scored":
        if validate_linux_release_binary(executable, release["sha256"]) != release:
            raise ValueError("OpenCode release identity changed before scored case")
        argv = build_scored_container_command(
            container_runtime,
            args.container_image,
            workspace,
            executable,
            prompt,
            provider_env,
            release["platform"],
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
        "redaction_materials": redaction_materials,
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


def run_case(
    batch_root,
    fixture,
    args,
    executable,
    provider_env,
    redaction_materials,
    release=None,
    preflight=None,
    container_runtime=None,
):
    context = case_context(
        batch_root,
        fixture,
        args,
        executable,
        provider_env,
        redaction_materials,
        release,
        container_runtime,
    )
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
            release["platform"],
        )
        probe_result = subprocess.run(
            probe_command,
            cwd=batch_root,
            env=container_client_environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        probe = {
            "command_argv": probe_command,
            "raw_returncode": probe_result.returncode,
            "stdout": redact_sensitive_values(
                probe_result.stdout, context["redaction_materials"]
            ),
            "stderr": redact_sensitive_values(
                probe_result.stderr, context["redaction_materials"]
            ),
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
        "release": release,
        "container_image": args.container_image if args.mode == "scored" else None,
        "preflight_version": preflight["version"] if preflight else None,
        "stdout": redact_sensitive_values(stdout, context["redaction_materials"]),
        "stderr": redact_sensitive_values(stderr, context["redaction_materials"]),
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


def write_result(stream, result, redaction_materials):
    line = json.dumps(result, ensure_ascii=False, sort_keys=True)
    line = redact_sensitive_values(line, redaction_materials)
    stream.write(line + "\n")
    stream.flush()
    os.fsync(stream.fileno())
    print(line, flush=True)


def main(argv=None):
    args = parse_args(argv)
    try:
        provider_env = provider_environment()
        redaction_materials = build_redaction_materials(
            provider_env, scored=args.mode == "scored"
        )
        executable = resolve_executable(args.opencode_bin)
        container_runtime = None
        release = None
        if args.mode == "scored":
            args.container_image = validate_container_image(args.container_image)
            release = validate_linux_release_binary(
                executable, args.opencode_sha256
            )
            container_runtime = resolve_container_runtime(args.container_runtime)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    batch_root = args.batch_root.resolve(strict=False)
    batch_root.mkdir(parents=True, exist_ok=False)
    preflight = None
    if args.mode == "scored":
        try:
            preflight = run_scored_preflight(
                container_runtime, args.container_image, executable, release
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        (batch_root / "preflight.json").write_text(
            json.dumps(preflight, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    selected_names = set(args.case or [])
    selected_cases = [case for case in CASE_NAMES if not selected_names or case in selected_names]
    results_path = batch_root / "results.jsonl"
    with results_path.open("x", encoding="utf-8") as stream:
        for fixture in selected_cases:
            try:
                result = run_case(
                    batch_root,
                    fixture,
                    args,
                    executable,
                    provider_env,
                    redaction_materials,
                    release,
                    preflight,
                    container_runtime,
                )
            except Exception as error:
                result = wrapper_failure_record(batch_root, fixture, args, error)
                write_result(stream, result, redaction_materials)
                continue
            write_result(stream, result, redaction_materials)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
