from __future__ import annotations

import json
import multiprocessing
import os
import queue
import re
import signal
import threading
from typing import Any, Callable, Dict, List, Optional, TypeVar

from .analyzer import JudgeClient
from .models import NodeJudgment, TraceNode, judgment_from_dict, stable_json

T = TypeVar("T")

CURRENT_NODE_PROMPT_CHARS = 1600
UPSTREAM_NODE_PROMPT_CHARS = 700


SYSTEM_PROMPT = """You are an offline root-cause attribution reviewer for agent semantic traces.
You must perform backward semantic taint analysis.

For the current node/component:
1. Decide whether this node's semantics contain a defect relevant to the objective.
2. If defective, decide whether the defect was mainly introduced by upstream nodes/components.
3. If not mainly caused by upstream defects, mark this node as a root-cause candidate.

Only use the trace facts provided. Do not invent unavailable trace facts.
Return a single JSON object. No markdown.
"""


class ClaudeJudgeClient(JudgeClient):
    def __init__(
        self,
        *,
        model: str = "",
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str = "",
        base_url_env: str = "ANTHROPIC_BASE_URL",
        max_tokens: int = 4096,
        repair_max_tokens: int = 1024,
        timeout_seconds: Optional[float] = None,
    ):
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "ClaudeJudgeClient requires the Anthropic Claude Python SDK. "
                "Install it with `python3 -m pip install anthropic` in your analysis environment."
            ) from exc
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing Claude API key. Set {api_key_env} before running trace attribution.")
        self.model = model or os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-5"
        self.base_url = base_url or os.environ.get(base_url_env) or ""
        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        timeout = timeout_seconds if timeout_seconds is not None else env_float("CLAUDE_TIMEOUT_SECONDS")
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self.api_key = api_key
        self.timeout_seconds = timeout
        self.client = Anthropic(**client_kwargs)
        self.max_tokens = max_tokens
        self.repair_max_tokens = repair_max_tokens

    def judge_node(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
    ) -> NodeJudgment:
        prompt = build_judgment_prompt(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
        )
        text = self._create_message_text(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
        )
        try:
            payload = parse_json_object(text)
        except ValueError:
            payload = self._repair_json_response(text=text, node=node)
        return judgment_from_dict(payload, node)

    def _repair_json_response(self, *, text: str, node: TraceNode) -> Dict[str, Any]:
        repaired = self._create_message_text(
            system=(
                "You repair malformed JSON emitted by an offline trace attribution reviewer. "
                "Return exactly one valid compact JSON object and no markdown. "
                "Do not introduce new trace facts."
            ),
            messages=[
                {
                    "role": "user",
                    "content": stable_json(
                        {
                            "malformed_output": text[:8000],
                            "fallback_node": {
                                "node_ref": node.ref,
                                "component": node.component,
                                "event_type": node.event_type,
                            },
                            "required_fields": [
                                "node_ref",
                                "component",
                                "event_type",
                                "has_defect",
                                "defect_type",
                                "defect_reason",
                                "influenced_by",
                                "is_root_cause",
                                "severity",
                                "confidence",
                                "model_notes",
                            ],
                            "repair_rules": [
                                "Preserve any clear judgment already present in malformed_output.",
                                "If a field is unavailable, use an empty string, false, unknown, 0.0, or [] as appropriate.",
                                "Use fallback_node values for node_ref, component, and event_type when missing.",
                            ],
                        }
                    ),
                }
            ],
            max_tokens=self.repair_max_tokens,
        )
        try:
            return parse_json_object(repaired)
        except ValueError as exc:
            raise ValueError(
                "Claude response did not contain a valid JSON object after repair. "
                f"Original: {text[:200]} Repaired: {repaired[:200]}"
            ) from exc

    def _create_message_text(self, *, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        if self.timeout_seconds is not None and self.timeout_seconds > 0:
            result = run_worker_with_timeout(
                anthropic_request_worker,
                {
                    "api_key": self.api_key,
                    "base_url": self.base_url,
                    "timeout_seconds": self.timeout_seconds,
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "system": system,
                    "messages": messages,
                },
                self.timeout_seconds,
            )
            return str(result.get("text") or "")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,
            system=system,
            messages=messages,
        )
        return response_text(response)


def build_judgment_prompt(
    *,
    node: TraceNode,
    upstream_nodes: List[TraceNode],
    downstream_context: List[str],
    objective: str,
) -> str:
    current_node = node.compact(max_chars=CURRENT_NODE_PROMPT_CHARS)
    compact_upstream_nodes = [item.compact(max_chars=UPSTREAM_NODE_PROMPT_CHARS) for item in upstream_nodes]
    schema = {
        "node_ref": node.ref,
        "component": node.component,
        "event_type": node.event_type,
        "has_defect": True,
        "defect_type": "short_snake_case_or_empty",
        "defect_reason": "why this node is or is not defective",
        "influenced_by": [
            {
                "upstream_ref": "record:...",
                "reason": "why this upstream node caused or propagated the defect",
                "confidence": 0.0,
            }
        ],
        "is_root_cause": False,
        "severity": "low|medium|high|unknown",
        "confidence": 0.0,
        "model_notes": "brief uncertainty or missing trace facts",
    }
    payload = {
        "objective": objective,
        "current_node": current_node,
        "upstream_nodes": compact_upstream_nodes,
        "downstream_taint_path": downstream_context,
        "prompt_compaction": {
            "current_node_max_chars": CURRENT_NODE_PROMPT_CHARS,
            "upstream_node_max_chars": UPSTREAM_NODE_PROMPT_CHARS,
            "upstream_node_count": len(upstream_nodes),
            "truncated_refs": [
                item.get("ref")
                for item in [current_node] + compact_upstream_nodes
                if isinstance(item, dict) and item.get("truncated")
            ],
        },
        "rules": [
            "If current_node.event_type is case.quality_gap, treat the quality gap as the defect to explain; do not answer that the evaluator itself is non-defective.",
            "For case.quality_gap, use missing_evidence, score, max_score, and upstream_nodes to decide which upstream component most likely introduced the quality gap.",
            "If the current node has no relevant semantic defect, set has_defect=false and influenced_by=[].",
            "If the current node is defective because upstream semantics are already defective, list only those upstream refs.",
            "If the current node first introduces the defect, set influenced_by=[] and is_root_cause=true.",
            "Prefer concrete trace refs from upstream_nodes. Do not cite refs that are absent from the supplied upstream list.",
        ],
        "required_json_schema": schema,
    }
    return stable_json(payload)


def response_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def parse_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Claude response did not contain a JSON object: {text[:200]}")
    return json.loads(match.group(0))


def call_with_wall_timeout(func: Callable[[], T], timeout_seconds: Optional[float]) -> T:
    if timeout_seconds is None or timeout_seconds <= 0:
        return func()
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return func()

    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(signum: int, frame: Any) -> None:
        raise TimeoutError(f"judge request exceeded wall timeout: {timeout_seconds}s")

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def run_worker_with_timeout(
    worker: Callable[[Dict[str, Any], Any], None],
    payload: Dict[str, Any],
    timeout_seconds: float,
) -> Dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=worker, args=(payload, result_queue))
    process.daemon = True
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(1)
        raise TimeoutError(f"judge worker exceeded wall timeout: {timeout_seconds}s")

    try:
        result = result_queue.get_nowait()
    except queue.Empty as exc:
        if process.exitcode and process.exitcode != 0:
            raise RuntimeError(f"judge worker exited with code {process.exitcode} without result") from exc
        raise RuntimeError("judge worker finished without result") from exc

    if not isinstance(result, dict):
        raise RuntimeError("judge worker returned invalid result")
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "judge worker failed"))
    return result


def anthropic_request_worker(payload: Dict[str, Any], result_queue: Any) -> None:
    try:
        from anthropic import Anthropic

        client_kwargs: Dict[str, Any] = {"api_key": payload["api_key"]}
        if payload.get("base_url"):
            client_kwargs["base_url"] = payload["base_url"]
        if payload.get("timeout_seconds"):
            client_kwargs["timeout"] = payload["timeout_seconds"]
        client = Anthropic(**client_kwargs)
        response = client.messages.create(
            model=payload["model"],
            max_tokens=payload["max_tokens"],
            temperature=payload.get("temperature", 0),
            system=payload["system"],
            messages=payload["messages"],
        )
        result_queue.put({"ok": True, "text": response_text(response)})
    except BaseException as exc:
        result_queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def env_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
