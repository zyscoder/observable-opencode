from __future__ import annotations

import json
import multiprocessing
import os
import queue
import re
import signal
import threading
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, TypeVar

from .analyzer import JudgeClient
from .cache import JudgmentCache, build_judge_cache_key
from .errors import (
    JudgeProviderError,
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
    is_provider_request_error,
    provider_failure_disposition_to_dict,
    provider_failure_fields,
    provider_failure_disposition,
    provider_failure_reason,
)
from .models import NodeJudgment, TraceNode, judgment_from_dict, stable_json

T = TypeVar("T")

CURRENT_NODE_PROMPT_CHARS = 1600
UPSTREAM_NODE_PROMPT_CHARS = 700
ARTIFACT_EVIDENCE_PROMPT_CHARS = 32000
DEFAULT_JUDGE_TIMEOUT_SECONDS = 60 * 60
JUDGMENT_PROMPT_SCHEMA_VERSION = "causal-judgment-v1"
ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION = "causal-root-confirmation-v1"


SYSTEM_PROMPT = """You are an offline root-cause attribution reviewer for agent semantic traces.
You must perform backward semantic taint analysis.

For the current node/component:
1. Decide whether this node's semantics contain a defect relevant to the objective.
2. If defective, decide whether the defect was mainly introduced by upstream nodes/components.
3. Classify the node as defect introduction, propagation, evidence, non-defective, or unknown.
4. Only a defect-introduction node may be marked as a root-cause candidate.

Only use the trace facts provided. Do not invent unavailable trace facts.
Return a single JSON object. No markdown.
"""
ROOT_CONFIRMATION_SYSTEM_PROMPT = (
    "You independently verify a proposed semantic-trace root cause. Try to falsify the proposal. "
    "Use only the current node's own recorded semantics. Return one JSON object and no markdown."
)


def resolve_thinking_config(base_url: str, thinking_mode: str, *, max_tokens: int) -> Optional[Dict[str, Any]]:
    mode = (thinking_mode or "auto").strip().lower()
    if mode not in {"auto", "enabled", "disabled"}:
        raise ValueError("thinking_mode must be auto, enabled, or disabled")
    if mode == "auto":
        if "api.deepseek.com" not in (base_url or "").lower():
            return None
        mode = "disabled"
    if mode == "disabled":
        return {"type": "disabled"}
    if max_tokens <= 1024:
        raise ValueError("thinking_mode=enabled requires max_tokens greater than 1024")
    return {"type": "enabled", "budget_tokens": min(4096, max_tokens - 1)}


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
        thinking_mode: str = "auto",
        cache_path: str = "",
        provider_error_threshold: int = 3,
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
        timeout = timeout_seconds if timeout_seconds is not None else default_judge_timeout_seconds()
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self.api_key = api_key
        self.timeout_seconds = timeout
        self.client = Anthropic(**client_kwargs)
        self.max_tokens = max_tokens
        self.repair_max_tokens = repair_max_tokens
        self.request_count = 0
        self.thinking_mode = thinking_mode or os.environ.get("CLAUDE_THINKING_MODE") or "auto"
        self.thinking_config = resolve_thinking_config(
            self.base_url,
            self.thinking_mode,
            max_tokens=self.max_tokens,
        )
        self.cache = JudgmentCache(Path(cache_path)) if cache_path else JudgmentCache()
        self.provider_error_threshold = max(1, int(provider_error_threshold))
        self.consecutive_provider_errors = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.provider_circuit_disposition = None
        self.provider_circuit_first_request = 0
        self.provider_circuit_first_failure_at = ""
        self.provider_circuit_previous_failure = None

    def judge_node(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
    ) -> NodeJudgment:
        return self._judge_node(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
            judgment_context=None,
        )

    def judge_node_with_context(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
        judgment_context: Dict[str, Any],
    ) -> NodeJudgment:
        return self._judge_node(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
            judgment_context=judgment_context,
        )

    def _judge_node(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
        judgment_context: Optional[Dict[str, Any]],
    ) -> NodeJudgment:
        allowed_upstream_refs = {item.ref for item in upstream_nodes}
        prompt = build_judgment_prompt(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
            judgment_context=judgment_context,
        )
        messages = [{"role": "user", "content": prompt}]
        cache_key = build_judge_cache_key(
            stage="node_judgment",
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=self.max_tokens,
            thinking_config=getattr(self, "thinking_config", None),
            prompt_schema_version=JUDGMENT_PROMPT_SCHEMA_VERSION,
        )
        cache = getattr(self, "cache", None)
        if cache:
            cached = cache.get(key=cache_key, node=node)
            if cached is not None:
                return cached
        text = self._create_message_text(
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=self.max_tokens,
        )
        try:
            payload = parse_json_object(text)
            validate_judgment_payload(
                payload,
                node=node,
                allowed_upstream_refs=allowed_upstream_refs,
            )
        except (TypeError, ValueError) as validation_error:
            try:
                payload = self._repair_json_response(
                    text=text,
                    node=node,
                    validation_error=validation_error,
                    allowed_upstream_refs=allowed_upstream_refs,
                )
            except ValueError as repair_error:
                try:
                    payload = self._retry_json_response(
                        prompt=prompt,
                        node=node,
                        malformed_text=text,
                        repair_error=repair_error,
                        allowed_upstream_refs=allowed_upstream_refs,
                    )
                except (TypeError, ValueError) as retry_error:
                    return exhausted_schema_unknown_judgment(node=node, error=retry_error)
        judgment = judgment_from_dict(payload, node)
        if cache:
            cache.put(
                key=cache_key,
                stage="node_judgment",
                model=self.model,
                node=node,
                judgment=judgment,
            )
        return judgment

    @property
    def cache_stats(self) -> Dict[str, Any]:
        cache = getattr(self, "cache", None)
        return cache.stats() if cache else {"enabled": False}

    @property
    def provider_circuit_stats(self) -> Dict[str, Any]:
        disposition = getattr(self, "provider_circuit_disposition", None)
        previous_failure = getattr(
            self, "provider_circuit_previous_failure", None
        )
        return {
            "threshold": getattr(self, "provider_error_threshold", 3),
            "consecutive_errors": getattr(self, "consecutive_provider_errors", 0),
            "open": getattr(self, "provider_circuit_open", False),
            "reason": getattr(self, "provider_circuit_reason", ""),
            "disposition": provider_failure_disposition_to_dict(disposition),
            "first_request": getattr(self, "provider_circuit_first_request", 0),
            "first_failure_at": getattr(
                self, "provider_circuit_first_failure_at", ""
            ),
            "previous_failure": (
                dict(previous_failure)
                if isinstance(previous_failure, Mapping)
                else None
            ),
        }

    def judge_evaluation_assertion(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
    ) -> NodeJudgment:
        return self.judge_node(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
        )

    def judge_evaluation_assertion_with_context(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
        judgment_context: Dict[str, Any],
    ) -> NodeJudgment:
        return self.judge_node_with_context(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=downstream_context,
            objective=objective,
            judgment_context=judgment_context,
        )

    def confirm_root(
        self,
        *,
        node: TraceNode,
        judgment: NodeJudgment,
        downstream_context: List[str],
        objective: str,
    ) -> NodeJudgment:
        prompt = build_root_confirmation_prompt(
            node=node,
            judgment=judgment,
            downstream_context=downstream_context,
            objective=objective,
        )
        messages = [{"role": "user", "content": prompt}]
        cache_key = build_judge_cache_key(
            stage="root_confirmation",
            model=self.model,
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            messages=messages,
            max_tokens=min(self.max_tokens, 2048),
            thinking_config=getattr(self, "thinking_config", None),
            prompt_schema_version=ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
        )
        cache = getattr(self, "cache", None)
        if cache:
            cached = cache.get(key=cache_key, node=node)
            if cached is not None:
                return cached
        text = self._create_message_text(
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            messages=messages,
            max_tokens=min(self.max_tokens, 2048),
        )
        try:
            payload = parse_json_object(text)
            validate_root_confirmation_payload(payload, node=node)
        except (TypeError, ValueError) as validation_error:
            repaired = self._create_message_text(
                system=(
                    "Repair an invalid root-confirmation JSON object. Correct the stated validation error "
                    "using only the supplied current-node facts. Return one JSON object and no markdown."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": stable_json(
                            {
                                "malformed_output": text[:8000],
                                "validation_error": f"{type(validation_error).__name__}: {validation_error}",
                                "current_node": node.compact(max_chars=CURRENT_NODE_PROMPT_CHARS),
                                "artifact_evidence": compact_artifact_evidence([node]),
                                "required_node_ref": node.ref,
                                "required_fields": {
                                    "node_ref": node.ref,
                                    "confirmation": "confirmed|rejected|unknown",
                                    "exact_semantic_excerpt": "verbatim current-node excerpt",
                                    "current_node_would_cause_defect_if_executed_exactly": False,
                                    "reason": "node-local reason",
                                    "confidence": 0.0,
                                },
                                "repair_rules": [
                                    "confirmed requires current_node_would_cause_defect_if_executed_exactly=true",
                                    "rejected requires current_node_would_cause_defect_if_executed_exactly=false",
                                    "The exact excerpt must be copied from the current node or hydrated artifact.",
                                ],
                            }
                        ),
                    }
                ],
                max_tokens=min(self.repair_max_tokens, 1024),
            )
            try:
                payload = parse_json_object(repaired)
                validate_root_confirmation_payload(payload, node=node)
            except (TypeError, ValueError) as repair_error:
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="unknown",
                    defect_type="root_confirmation_error",
                    defect_reason="The proposed root could not be independently confirmed.",
                    causal_role="unknown",
                    branch_relation="unknown",
                    influenced_by=[],
                    is_root_cause=False,
                    severity="unknown",
                    confidence=0.0,
                    model_notes=(
                        f"root confirmation invalid: {type(validation_error).__name__}: {validation_error}; "
                        f"repair invalid: {type(repair_error).__name__}: {repair_error}"
                    ),
                )
        confirmation = str(payload.get("confirmation") or "").strip().lower()
        reason = str(payload.get("reason") or "").strip()
        confidence = float(payload.get("confidence") or 0.0)
        if confirmation == "confirmed":
            notes = "root independently confirmed"
            if judgment.model_notes:
                notes = f"{judgment.model_notes}; {notes}"
            result = replace(
                judgment,
                confidence=min(judgment.confidence, confidence) if judgment.confidence else confidence,
                model_notes=notes,
            )
        elif confirmation == "rejected":
            result = NodeJudgment(
                node_ref=node.ref,
                component=node.component,
                event_type=node.event_type,
                has_defect=False,
                defect_status="absent",
                defect_type="",
                defect_reason=reason,
                causal_role="non_defective",
                branch_relation="unrelated",
                influenced_by=[],
                is_root_cause=False,
                severity="unknown",
                confidence=confidence,
                model_notes="proposed root rejected by independent node-local confirmation",
            )
        else:
            result = NodeJudgment(
                node_ref=node.ref,
                component=node.component,
                event_type=node.event_type,
                has_defect=False,
                defect_status="unknown",
                defect_type="root_confirmation_unknown",
                defect_reason=reason,
                causal_role="unknown",
                branch_relation="unknown",
                influenced_by=[],
                is_root_cause=False,
                severity="unknown",
                confidence=confidence,
                model_notes="root confirmation lacked decisive node-local evidence",
            )
        if cache:
            cache.put(
                key=cache_key,
                stage="root_confirmation",
                model=self.model,
                node=node,
                judgment=result,
            )
        return result

    def _retry_json_response(
        self,
        *,
        prompt: str,
        node: TraceNode,
        malformed_text: str,
        repair_error: Exception,
        allowed_upstream_refs: Set[str],
    ) -> Dict[str, Any]:
        retried = self._create_message_text(
            system=(
                SYSTEM_PROMPT
                + "\nA previous response and its repair were malformed. Return all required fields in one complete JSON object."
            ),
            messages=[
                {"role": "user", "content": prompt},
                {
                    "role": "user",
                    "content": stable_json(
                        {
                            "retry_reason": f"{type(repair_error).__name__}: {repair_error}",
                            "malformed_output_preview": malformed_text[:1000],
                            "required_node_ref": node.ref,
                        }
                    ),
                },
            ],
            max_tokens=self.max_tokens,
        )
        payload = parse_json_object(retried)
        validate_judgment_payload(
            payload,
            node=node,
            allowed_upstream_refs=allowed_upstream_refs,
        )
        return payload

    def _repair_json_response(
        self,
        *,
        text: str,
        node: TraceNode,
        validation_error: Exception,
        allowed_upstream_refs: Set[str],
    ) -> Dict[str, Any]:
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
                            "validation_error": f"{type(validation_error).__name__}: {validation_error}",
                            "allowed_upstream_refs": sorted(allowed_upstream_refs),
                            "fallback_node": {
                                "node_ref": node.ref,
                                "component": node.component,
                                "event_type": node.event_type,
                                "semantic_role": semantic_role_for_node(node),
                            },
                            "required_fields": [
                                "node_ref",
                                "component",
                                "event_type",
                                "defect_status",
                                "has_defect",
                                "defect_type",
                                "defect_reason",
                                "causal_role",
                                "branch_relation",
                                "influenced_by",
                                "is_root_cause",
                                "severity",
                                "confidence",
                                "model_notes",
                            ],
                            "repair_rules": [
                                "Correct the specific validation_error before returning the repaired judgment.",
                                "Preserve any clear judgment already present in malformed_output.",
                                "defect_status must be present, absent, or unknown; use unknown when the evidence is insufficient.",
                                "When defect_status is absent, defect_type must be an empty string and the reason must not describe the current node as defective.",
                                "defect_reason must explain the judgment or the uncertainty and must not be empty.",
                                "causal_role must be defect_introduction, defect_propagation, defect_evidence, non_defective, or unknown.",
                                "branch_relation must be same_defect, causal_precursor, outcome_evidence, unrelated, or unknown.",
                                "Each influenced_by item must classify relation as defect_propagated_from, motivated_by_evidence, or derived_from.",
                                "Every influenced_by.upstream_ref must be one of allowed_upstream_refs; never emit unknown, none, or a fabricated ref.",
                                "A defect_propagated_from reason must state how the upstream already contains the same defect; mere motivation or evidence requires motivated_by_evidence.",
                                "A faithful test or tool result that exposes a failure is defect_evidence, not defect_introduction.",
                                "A repository problem or missing implementation is a task precondition, not the same defect as an Agent failure to act or an empty patch.",
                                "For canonical decisions, an LLM generation envelope that produced the same rationale is derived_from, not defect propagation.",
                                "An authored_agent_action is never outcome evidence: a defective action is introduction or propagation, and a correct action is non-defective.",
                                "A defect-introduction root may retain motivated_by_evidence or derived_from influences, but never defect_propagated_from.",
                                "If another field is unavailable, use an empty string, false, unknown, 0.0, or [] as appropriate.",
                                "Use fallback_node values for node_ref, component, and event_type when missing.",
                            ],
                        }
                    ),
                }
            ],
            max_tokens=self.repair_max_tokens,
        )
        try:
            payload = parse_json_object(repaired)
            validate_judgment_payload(
                payload,
                node=node,
                allowed_upstream_refs=allowed_upstream_refs,
            )
            return payload
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Claude response did not contain a complete judgment object after repair. "
                f"Original validation: {type(validation_error).__name__}: {validation_error}. "
                f"Repair validation: {type(exc).__name__}: {exc}. "
                f"Original: {text[:200]} Repaired: {repaired[:200]}"
            ) from exc

    def create_message_text_with_usage(
        self, *, system: str, messages: List[Dict[str, str]], max_tokens: int
    ) -> TransportCallResult:
        if getattr(self, "provider_circuit_open", False):
            raise TransportCallError(
                JudgeProviderUnavailable(
                    getattr(self, "provider_circuit_reason", "provider circuit is open")
                ),
                physical_requests=0,
            )
        try:
            timeout_seconds = self.timeout_seconds
            if timeout_seconds is not None and timeout_seconds > 0:
                worker_arguments = {
                    "api_key": self.api_key,
                    "base_url": self.base_url,
                    "timeout_seconds": timeout_seconds,
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "thinking": self.thinking_config,
                    "system": system,
                    "messages": messages,
                }
                direct_create = None
                request = None
            else:
                worker_arguments = None
                direct_create = self.client.messages.create
                request = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "system": system,
                    "messages": messages,
                }
                if self.thinking_config is not None:
                    request["thinking"] = self.thinking_config
        except Exception as exc:
            raise TransportCallError(exc, physical_requests=0) from exc

        self.request_count += 1
        try:
            if worker_arguments is not None:
                result = run_worker_with_timeout(
                    anthropic_request_worker,
                    worker_arguments,
                    timeout_seconds,
                )
                text = str(result.get("text") or "")
            else:
                response = direct_create(**request)
                text = response_text(response)
            self.consecutive_provider_errors = 0
            self.provider_circuit_reason = ""
            self.provider_circuit_disposition = None
            self.provider_circuit_first_request = 0
            self.provider_circuit_first_failure_at = ""
            return TransportCallResult(text=text, physical_requests=1)
        except BaseException as exc:
            disposition = provider_failure_disposition(exc)
            if disposition is None:
                if isinstance(exc, Exception):
                    raise TransportCallError(exc, physical_requests=1) from exc
                raise
            self.provider_circuit_disposition = disposition
            if not getattr(self, "provider_circuit_first_request", 0):
                self.provider_circuit_first_request = self.request_count
                self.provider_circuit_first_failure_at = datetime.now(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z")
            if not disposition.retryable:
                self.provider_circuit_open = True
                self.provider_circuit_reason = "non-retryable Provider failure: {0}".format(
                    disposition.reason
                )
                raise TransportCallError(
                    JudgeProviderUnavailable(self.provider_circuit_reason),
                    physical_requests=1,
                ) from exc
            consecutive = getattr(self, "consecutive_provider_errors", 0) + 1
            self.consecutive_provider_errors = consecutive
            threshold = max(1, int(getattr(self, "provider_error_threshold", 3)))
            detail = f"{type(exc).__name__}: {exc}"
            if consecutive >= threshold:
                self.provider_circuit_open = True
                self.provider_circuit_reason = (
                    f"provider unavailable after {consecutive} consecutive request errors: {detail}"
                )
                error = JudgeProviderUnavailable(self.provider_circuit_reason)
            else:
                error = JudgeProviderError(detail)
            raise TransportCallError(error, physical_requests=1) from exc

    def _create_message_text(self, *, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        try:
            return self.create_message_text_with_usage(
                system=system, messages=messages, max_tokens=max_tokens
            ).text
        except TransportCallError as exc:
            raise exc.error from exc

    def create_message_text(
        self, *, system: str, messages: List[Dict[str, str]], max_tokens: int
    ) -> str:
        return self._create_message_text(system=system, messages=messages, max_tokens=max_tokens)


def build_judgment_prompt(
    *,
    node: TraceNode,
    upstream_nodes: List[TraceNode],
    downstream_context: List[str],
    objective: str,
    judgment_context: Optional[Dict[str, Any]] = None,
) -> str:
    current_node = node.compact(max_chars=CURRENT_NODE_PROMPT_CHARS)
    compact_upstream_nodes = [item.compact(max_chars=UPSTREAM_NODE_PROMPT_CHARS) for item in upstream_nodes]
    artifact_evidence = compact_artifact_evidence([node] + upstream_nodes)
    schema = {
        "node_ref": node.ref,
        "component": node.component,
        "event_type": node.event_type,
        "defect_status": "present|absent|unknown",
        "has_defect": True,
        "defect_type": "short_snake_case_or_empty",
        "defect_reason": "why this node is or is not defective",
        "causal_role": "defect_introduction|defect_propagation|defect_evidence|non_defective|unknown",
        "branch_relation": "same_defect|causal_precursor|outcome_evidence|unrelated|unknown",
        "influenced_by": [
            {
                "upstream_ref": "record:...",
                "reason": "how this upstream node relates to the current semantics",
                "relation": "defect_propagated_from|motivated_by_evidence|derived_from",
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
        "current_node_semantic_role": semantic_role_for_node(node),
        "upstream_nodes": compact_upstream_nodes,
        "artifact_evidence": artifact_evidence,
        "active_defect_branch": {
            "observed_defect": downstream_context[0] if downstream_context else "",
            "path": downstream_context,
        },
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
            "A case.quality_gap, case.observed_defect, or case.missing_semantic node is an evaluation assertion to validate against supplied upstream trace facts; the assertion may be rejected when those facts contradict it.",
            "A progress.episode node is an offline aggregate for navigation and evidence. It may describe defective progress but can never be a defect-introduction root; identify a concrete member decision or action instead.",
            "For an evaluation assertion, use its dimensions and upstream_nodes to determine whether the asserted defect is present, absent, or unknown.",
            "If the current node has no relevant semantic defect, set defect_status=absent, has_defect=false, and influenced_by=[].",
            "defect_status classifies the current node's semantics, not whether it is the code-level root. A false or unsupported response claim is present even when an earlier code change caused the underlying failure.",
            "When the active branch starts at a response.claim or response.output instead of an evaluation assertion, evaluate answer quality only; do not treat the repository problem described by the answer as an observed Agent defect.",
            "A truthful response claim that identifies a pre-existing requirement/code conflict, selects the current requirement over a superseded document, reports its repair, or reports a confirmed verification is non_defective rather than defect_evidence.",
            "Mentioning, analyzing, or fixing a repository defect does not make the response node defective. Mark a supported response claim present only when its own conclusion contradicts, misuses, or overstates the supplied evidence.",
            "Use defect_evidence for a truthful verification, tool result, benchmark result, or observation that exposes a defect without introducing it.",
            "Use defect_introduction only when this node first introduces the defect and no earlier supplied causal node did so.",
            "Classify branch_relation relative to active_defect_branch; unrelated defects must not become roots for this branch.",
            "A repository problem or missing implementation is a task precondition. For a failure to act, repeated exploration, or empty-patch defect, it is evidence, not propagation.",
            "Prioritize rationale and executable arguments over generic orchestration labels; duplicate LLM generation is derived_from, not propagation.",
            "A truthful environment or tool result may motivate a decision but does not propagate the decision's defect; label that edge motivated_by_evidence.",
            "Authored tool-call arguments or test scripts are action semantics; their execution results are outcome evidence unless the result itself corrupts data.",
            "For an authored_agent_action, judge the executable arguments or script as the current node's semantics. A defective authored action is defect_introduction or defect_propagation, never defect_evidence.",
            "A generic intent to run a test does not introduce a defect contained only in a later authored_agent_action.",
            "A correct current plan is not defective merely because a later action in the same episode is defective; judge each node's own semantics before grouping the episode.",
            "Code size or complexity alone never proves defect introduction; require a concrete semantic mismatch visible in supplied trace facts.",
            "A defect_evidence or defect_propagation node must never be marked is_root_cause=true.",
            "Never set defect_status=absent while using a non-empty defect_type or while describing the current node as a semantic defect.",
            "If the supplied facts are insufficient to decide, set defect_status=unknown, has_defect=false, is_root_cause=false, and explain what is missing.",
            "Treat hydrated_artifacts as the full cited trace payload within its recorded truncation boundary; do not discard it in favor of a shorter preview.",
            "When a hydrated artifact is marked truncated, you must not infer a defect or root cause from the missing portion; return unknown if the visible excerpt is not independently decisive.",
            "Compare verification results only within the relevant repository_revision. A superseded or historical failure does not contradict an effective current-revision pass.",
            "For response.claim nodes, inspect direct_support_refs before candidate_context_refs and inspect superseded_evidence_refs last.",
            "Use motivated_by_evidence or derived_from for non-defective provenance; these relations do not carry defect taint backward.",
            "If the current node first introduces the defect, set defect_status=present, has_defect=true, is_root_cause=true, and include no defect_propagated_from influence.",
            "Prefer concrete trace refs from upstream_nodes. Do not cite refs that are absent from the supplied upstream list.",
        ],
        "required_json_schema": schema,
    }
    if judgment_context:
        payload["causal_judgment_context"] = judgment_context
        payload["rules"].extend(
            [
                "Treat causal_judgment_context edge relations as recorded provenance",
                "Use downstream_judgments to preserve active-defect identity, but independently judge the current node's own semantics.",
                "An incoming edge records provenance, not automatic defect propagation; only defect_propagated_from carries taint backward.",
            ]
        )
    return stable_json(payload)


def build_root_confirmation_prompt(
    *,
    node: TraceNode,
    judgment: NodeJudgment,
    downstream_context: List[str],
    objective: str,
) -> str:
    return stable_json(
        {
            "objective": objective,
            "active_defect_branch": downstream_context,
            "proposed_root_judgment": {
                "node_ref": judgment.node_ref,
                "defect_type": judgment.defect_type,
                "defect_reason": judgment.defect_reason,
                "confidence": judgment.confidence,
            },
            "current_node": node.compact(max_chars=CURRENT_NODE_PROMPT_CHARS),
            "current_node_semantic_role": semantic_role_for_node(node),
            "artifact_evidence": compact_artifact_evidence([node]),
            "rules": [
                "Try to falsify the proposed root instead of repeating the first judgment.",
                "Use only the current node's own semantics; a downstream defect or later action cannot make a correct current node defective.",
                "A confirmed root requires an exact excerpt from the current node that contains the defective choice.",
                "Apply the counterfactual: set current_node_would_cause_defect_if_executed_exactly=true only when executing this node exactly would itself cause the active defect.",
                "Reject the root when the exact excerpt describes the opposite of the observed defect.",
                "Reject the root when the excerpt truthfully describes a pre-existing repository defect or its correct resolution rather than introducing an Agent behavior defect.",
                "Reject the root when the excerpt concerns an adjacent but different mechanism from the active defect branch.",
                "For an Agent failure-to-act branch, a repository gap is a task precondition; the root must contain the defective choice to delay, abandon, or avoid required action.",
                "Return unknown when the visible node-local evidence is insufficient or truncated at the decisive point.",
            ],
            "required_json_schema": {
                "node_ref": node.ref,
                "confirmation": "confirmed|rejected|unknown",
                "exact_semantic_excerpt": "verbatim excerpt from current node, empty only for unknown",
                "current_node_would_cause_defect_if_executed_exactly": False,
                "reason": "node-local confirmation or rejection reason",
                "confidence": 0.0,
            },
        }
    )


def validate_root_confirmation_payload(value: Dict[str, Any], *, node: TraceNode) -> None:
    if not isinstance(value, dict):
        raise TypeError("root confirmation payload must be an object")
    if str(value.get("node_ref") or "").strip() != node.ref:
        raise ValueError("root confirmation node_ref does not match the current node")
    confirmation = str(value.get("confirmation") or "").strip().lower()
    if confirmation not in {"confirmed", "rejected", "unknown"}:
        raise ValueError("root confirmation must be confirmed, rejected, or unknown")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("root confirmation requires a reason")
    causation_field = "current_node_would_cause_defect_if_executed_exactly"
    if not isinstance(value.get(causation_field), bool):
        raise ValueError("root confirmation requires a boolean counterfactual result")
    if not isinstance(value.get("confidence"), (int, float)) or isinstance(value.get("confidence"), bool):
        raise ValueError("root confirmation requires numeric confidence")
    if confirmation == "rejected" and value[causation_field]:
        raise ValueError("rejected root requires a false counterfactual causation result")
    if confirmation != "confirmed":
        return
    if not value[causation_field]:
        raise ValueError("confirmed root requires a true counterfactual defect result")
    excerpt = str(value.get("exact_semantic_excerpt") or "").strip()
    if not excerpt:
        raise ValueError("confirmed root requires an exact semantic excerpt")
    corpus = normalize_semantic_text(" ".join(semantic_text_fragments(node.data)))
    if not semantic_excerpt_is_grounded(excerpt, corpus):
        raise ValueError("exact semantic excerpt is not present in the current node")


def normalize_semantic_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def semantic_excerpt_is_grounded(excerpt: str, normalized_corpus: str) -> bool:
    normalized_excerpt = normalize_semantic_text(excerpt)
    if normalized_excerpt in normalized_corpus:
        return True
    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?", excerpt)
    stop_words = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "uses",
        "use",
        "dispatches",
        "calls",
        "requires",
        "require",
    }
    significant = [token for token in raw_tokens if token.lower() not in stop_words]
    if len(significant) < 2:
        return False
    corpus_tokens = set(re.findall(r"[a-z_][a-z0-9_]*|\d+(?:\.\d+)?", normalized_corpus))
    grounded = [token for token in significant if token.lower() in corpus_tokens]
    code_grounded = [
        token
        for token in grounded
        if "_" in token or any(character.isupper() for character in token[1:]) or any(character.isdigit() for character in token)
    ]
    return len(grounded) >= 2 and len(grounded) / len(significant) >= 0.6 and bool(code_grounded)


def semantic_text_fragments(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        output: List[str] = []
        for item in value.values():
            output.extend(semantic_text_fragments(item))
        return output
    if isinstance(value, (list, tuple)):
        output = []
        for item in value:
            output.extend(semantic_text_fragments(item))
        return output
    return []


def semantic_role_for_node(node: TraceNode) -> str:
    if node.event_type == "progress.episode":
        return "offline_progress_aggregate"
    if is_authored_agent_action(node):
        return "authored_agent_action"
    if node.event_type == "decision" and str(node.data.get("decision_type") or "") == "reasoning_block":
        return "agent_reasoning_or_plan"
    if node.event_type in ("tool.result", "tool.error", "verification", "execution.observation"):
        return "execution_outcome"
    if node.event_type in ("case.observed_defect", "case.quality_gap", "case.missing_semantic"):
        return "offline_evaluation_assertion"
    if node.event_type in ("evidence.fact", "evidence.semantic_fact"):
        return "observed_semantic_fact"
    return "trace_record"


def exhausted_schema_unknown_judgment(*, node: TraceNode, error: Exception) -> NodeJudgment:
    detail = f"{type(error).__name__}: {error}"
    return NodeJudgment(
        node_ref=node.ref,
        component=node.component,
        event_type=node.event_type,
        has_defect=False,
        defect_status="unknown",
        defect_type="judge_schema_error",
        defect_reason="The model judgment remained structurally invalid after repair and retry.",
        causal_role="unknown",
        branch_relation="unknown",
        influenced_by=[],
        is_root_cause=False,
        severity="unknown",
        confidence=0.0,
        model_notes=f"schema repair exhausted: {detail}",
    )


def is_authored_agent_action(node: TraceNode) -> bool:
    if node.event_type != "decision":
        return False
    decision_type = str(node.data.get("decision_type") or "").strip().lower()
    if decision_type in {"llm_tool_call", "agent_tool_call", "tool_call"}:
        return True
    intent = str(node.data.get("intent") or "").strip().lower()
    return intent == "model requested tool execution"


def compact_artifact_evidence(nodes: List[TraceNode], max_chars: int = ARTIFACT_EVIDENCE_PROMPT_CHARS) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    remaining = max_chars
    for node in nodes:
        artifacts = node.data.get("hydrated_artifacts") if isinstance(node.data, dict) else None
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict) or remaining <= 0:
                continue
            content = str(artifact.get("content") or "")
            limit = min(16000, remaining)
            excerpt = content[:limit]
            output.append(
                {
                    "node_ref": node.ref,
                    "artifact_id": artifact.get("artifact_id"),
                    "kind": artifact.get("kind"),
                    "label": artifact.get("label"),
                    "path": artifact.get("path"),
                    "hash": artifact.get("hash"),
                    "content": excerpt,
                    "content_length": artifact.get("content_length"),
                    "trace_artifact_truncated": bool(artifact.get("truncated")),
                    "prompt_excerpt_truncated": len(content) > len(excerpt),
                }
            )
            remaining -= len(excerpt)
    return output


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


def validate_judgment_payload(
    value: Dict[str, Any],
    *,
    node: Optional[TraceNode] = None,
    allowed_upstream_refs: Optional[Set[str]] = None,
) -> None:
    if not isinstance(value, dict):
        raise TypeError("judgment payload must be an object")
    raw_status = value.get("defect_status")
    status = str(raw_status or "").strip().lower()
    if raw_status is not None and status not in {"present", "absent", "unknown"}:
        raise ValueError("defect_status must be present, absent, or unknown")
    if status not in {"present", "absent", "unknown"} and not isinstance(value.get("has_defect"), bool):
        raise ValueError("judgment requires defect_status or a legacy boolean has_defect")
    if status in {"present", "absent", "unknown"} and isinstance(value.get("has_defect"), bool):
        expected_has_defect = status == "present"
        if value["has_defect"] != expected_has_defect:
            raise ValueError("defect_status and has_defect are inconsistent")
    if status == "absent" and str(value.get("defect_type") or "").strip():
        raise ValueError("absent judgments must use an empty defect_type")
    causal_role = str(value.get("causal_role") or "").strip().lower()
    valid_roles = {
        "defect_introduction",
        "defect_propagation",
        "defect_evidence",
        "non_defective",
        "unknown",
    }
    if causal_role and causal_role not in valid_roles:
        raise ValueError("invalid causal_role")
    reason = value.get("defect_reason") or value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("judgment requires a non-empty defect_reason")
    if not isinstance(value.get("influenced_by"), list):
        raise ValueError("judgment requires influenced_by as a list")
    influences = value.get("influenced_by") or []
    if any(not isinstance(item, dict) for item in influences):
        raise ValueError("each influenced_by item must be an object")
    influence_relations = []
    for item in influences:
        upstream_ref = str(item.get("upstream_ref") or "").strip()
        if not upstream_ref or upstream_ref.lower() in {"unknown", "none", "null", "n/a"}:
            raise ValueError("influenced_by requires a concrete upstream_ref")
        if allowed_upstream_refs is not None and upstream_ref not in allowed_upstream_refs:
            raise ValueError(
                f"influenced_by upstream_ref {upstream_ref} is not in the supplied upstream node set"
            )
        influence_reason = str(item.get("reason") or "").strip()
        if not influence_reason:
            raise ValueError("influenced_by requires a non-empty reason")
        relation = str(item.get("relation") or "").strip().lower()
        influence_relations.append(relation)
        if (
            node
            and node.event_type == "decision"
            and relation == "defect_propagated_from"
            and reason_describes_duplicate_generation_envelope(influence_reason)
        ):
            raise ValueError(
                "a canonical decision cannot propagate its defect from the duplicate LLM generation envelope"
            )
        if relation == "defect_propagated_from" and propagation_reason_only_describes_motivation(
            influence_reason
        ):
            raise ValueError(
                "defect_propagated_from reason describes motivation/evidence rather than an upstream copy "
                "of the same defect; use motivated_by_evidence"
            )
    if any(
        relation not in {"defect_propagated_from", "motivated_by_evidence", "derived_from"}
        for relation in influence_relations
    ):
        raise ValueError("invalid influence relation")
    if not isinstance(value.get("is_root_cause"), bool):
        raise ValueError("judgment requires is_root_cause as a boolean")
    if causal_role in {"defect_evidence", "defect_propagation", "non_defective", "unknown"} and value.get(
        "is_root_cause"
    ):
        raise ValueError(f"{causal_role} cannot be marked as a root cause")
    if node and node.event_type == "progress.episode" and (
        causal_role == "defect_introduction" or value.get("is_root_cause")
    ):
        raise ValueError("an offline progress episode cannot be marked as a root cause")
    if node and is_authored_agent_action(node) and status == "present" and causal_role == "defect_evidence":
        raise ValueError(
            "authored action semantics cannot be classified as defect_evidence; "
            "use defect_introduction or defect_propagation"
        )
    if causal_role == "defect_introduction":
        if (
            status != "present"
            or not value.get("is_root_cause")
            or "defect_propagated_from" in influence_relations
        ):
            raise ValueError("defect_introduction requires a present root with no defect predecessor")
        branch_relation = str(value.get("branch_relation") or "same_defect").strip().lower()
        if branch_relation not in {"same_defect", "causal_precursor"}:
            raise ValueError("defect_introduction requires same_defect or causal_precursor branch_relation")
        if speculative_root_reason(reason):
            raise ValueError("root cause reason is speculative and lacks a concrete defect mechanism")
    if causal_role == "defect_propagation":
        propagated = [
            item
            for item in value.get("influenced_by") or []
            if isinstance(item, dict)
            and str(item.get("relation") or "defect_propagated_from").strip().lower()
            == "defect_propagated_from"
        ]
        if not propagated:
            raise ValueError("defect_propagation requires a defect_propagated_from influence")
    if causal_role == "non_defective" and status != "absent":
        raise ValueError("non_defective requires defect_status=absent")
    if causal_role == "unknown" and status != "unknown":
        raise ValueError("unknown causal_role requires defect_status=unknown")
    if not isinstance(value.get("confidence"), (int, float)) or isinstance(value.get("confidence"), bool):
        raise ValueError("judgment requires numeric confidence")


def speculative_root_reason(reason: str) -> bool:
    return bool(re.search(r"\b(could|may|might|possibly|potentially|perhaps)\b", reason, flags=re.IGNORECASE))


def propagation_reason_only_describes_motivation(reason: str) -> bool:
    normalized = reason.lower()
    motivation_terms = ("motivat", "prompted", "triggered", "evidence", "exposed")
    same_defect_terms = (
        "same defect",
        "already contains",
        "already carried",
        "pre-existing defect",
        "propagates the defect",
        "transmits the defect",
    )
    return any(term in normalized for term in motivation_terms) and not any(
        term in normalized for term in same_defect_terms
    )


def reason_describes_duplicate_generation_envelope(reason: str) -> bool:
    normalized = reason.lower()
    if "llm call" not in normalized:
        return False
    return any(
        phrase in normalized
        for phrase in (
            "produced the reasoning",
            "generated the reasoning",
            "produced this reasoning",
            "source of the rationale",
        )
    )


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
        raise_worker_failure(result)
    return result


def raise_worker_failure(result: Mapping[str, Any]) -> None:
    """Rehydrate structured Provider fields emitted by the timeout worker."""
    error = str(result.get("error") or "judge worker failed")
    error_type = str(result.get("error_type") or "")
    status = result.get("status_code")
    code = result.get("error_code")
    if type(status) is int or code or is_provider_request_error(
        RuntimeError(f"{error_type}: {error}")
    ):
        provider_error = JudgeProviderError(error)
        provider_error.status_code = status if type(status) is int else None
        provider_error.code = str(code or "")
        raise provider_error
    raise RuntimeError(error)


def anthropic_request_worker(payload: Dict[str, Any], result_queue: Any) -> None:
    try:
        from anthropic import Anthropic

        client_kwargs: Dict[str, Any] = {"api_key": payload["api_key"]}
        if payload.get("base_url"):
            client_kwargs["base_url"] = payload["base_url"]
        if payload.get("timeout_seconds"):
            client_kwargs["timeout"] = payload["timeout_seconds"]
        client = Anthropic(**client_kwargs)
        request: Dict[str, Any] = {
            "model": payload["model"],
            "max_tokens": payload["max_tokens"],
            "temperature": payload.get("temperature", 0),
            "system": payload["system"],
            "messages": payload["messages"],
        }
        if payload.get("thinking") is not None:
            request["thinking"] = payload["thinking"]
        response = client.messages.create(**request)
        result_queue.put({"ok": True, "text": response_text(response)})
    except BaseException as exc:
        status_code, error_code = provider_failure_fields(exc)
        result_queue.put(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": provider_failure_reason(exc),
                "status_code": status_code,
                "error_code": str(error_code or ""),
            }
        )


def env_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def default_judge_timeout_seconds() -> float:
    return env_float("CLAUDE_TIMEOUT_SECONDS") or DEFAULT_JUDGE_TIMEOUT_SECONDS
