from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .analyzer import JudgeClient
from .models import NodeJudgment, TraceNode, judgment_from_dict, stable_json


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
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response_text(response)
        try:
            payload = parse_json_object(text)
        except ValueError:
            payload = self._repair_json_response(text=text, node=node)
        return judgment_from_dict(payload, node)

    def _repair_json_response(self, *, text: str, node: TraceNode) -> Dict[str, Any]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.repair_max_tokens,
            temperature=0,
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
        )
        repaired = response_text(response)
        try:
            return parse_json_object(repaired)
        except ValueError as exc:
            raise ValueError(
                "Claude response did not contain a valid JSON object after repair. "
                f"Original: {text[:200]} Repaired: {repaired[:200]}"
            ) from exc


def build_judgment_prompt(
    *,
    node: TraceNode,
    upstream_nodes: List[TraceNode],
    downstream_context: List[str],
    objective: str,
) -> str:
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
        "current_node": node.compact(),
        "upstream_nodes": [item.compact(max_chars=1200) for item in upstream_nodes],
        "downstream_taint_path": downstream_context,
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


def env_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
