from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .cache import JudgmentCache, build_judge_cache_key
from .claude import parse_json_object
from .graph import TraceGraph
from .models import stable_json


DEFECT_EVOLUTION_SCHEMA_VERSION = "defect-evolution/v1"
DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION = "defect-explanation-prompt/v1"


_STAGE_TITLES = {
    "contract_established": "行为契约形成",
    "context_delivery": "契约进入模型上下文",
    "defect_introduction": "缺陷首次引入",
    "defect_propagation": "缺陷继续传播",
    "defect_materialization": "缺陷转化为动作",
    "late_recovery": "迟到的补偿动作",
    "user_observation": "用户观察到偏差",
}


def explanation_output_path(report_path: Path) -> Path:
    path = Path(report_path)
    return path.with_name("{0}.explanation.md".format(path.stem))


def build_defect_evolution(
    report: Mapping[str, Any],
    graph: TraceGraph,
) -> dict[str, Any]:
    """Project a confirmed causal path into an auditable semantic timeline."""
    roots = _mapping_items(report.get("confirmed_roots"))
    if not roots:
        roots = _mapping_items(report.get("co_roots"))
    root = roots[0] if roots else {}
    root_ref = _grounded_ref(graph, root.get("node_ref"))
    premise = _question_premise(report)
    contract_refs = _grounded_refs(graph, premise.get("contract_source_refs"))
    causal_path = _root_causal_path(report, graph, root, root_ref)
    observation_ref = causal_path[-1] if len(causal_path) > 1 else ""
    path_body = [
        ref for ref in causal_path[1:-1] if ref not in contract_refs
    ]
    actual_sequence = _grounded_refs(graph, premise.get("actual_sequence_refs"))
    next_actions = _contract_next_actions(graph, contract_refs)
    late_recovery_ref = _late_recovery_ref(
        graph,
        actual_sequence,
        path_body=path_body,
        observation_ref=observation_ref,
        next_actions=next_actions,
        excluded_refs=frozenset([*contract_refs, *causal_path]),
    )

    steps: list[dict[str, Any]] = []
    previous_ref = ""
    for index, ref in enumerate(contract_refs):
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=ref,
                stage="contract_established" if index == 0 else "context_delivery",
                input_refs=_grounded_refs(graph, graph.upstream_refs(ref)),
                defect_before="absent",
                defect_after="absent",
            )
        )
        previous_ref = ref
    for ref in _root_context_refs(graph, root_ref):
        if ref in contract_refs:
            continue
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=ref,
                stage="context_delivery",
                input_refs=_step_input_refs(graph, ref, previous_ref),
                defect_before="absent",
                defect_after="absent",
            )
        )
        previous_ref = ref
    if root_ref:
        input_refs = _grounded_refs(graph, graph.upstream_refs(root_ref))
        if previous_ref and previous_ref not in input_refs:
            input_refs.append(previous_ref)
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=root_ref,
                stage="defect_introduction",
                input_refs=input_refs,
                defect_before="absent",
                defect_after="present",
            )
        )
        previous_ref = root_ref
    for index, ref in enumerate(path_body):
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=ref,
                stage=(
                    "defect_materialization"
                    if index == 0
                    else "defect_propagation"
                ),
                input_refs=_step_input_refs(graph, ref, previous_ref),
                defect_before="present" if index == 0 else "propagated",
                defect_after="propagated",
            )
        )
        previous_ref = ref
    if late_recovery_ref:
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=late_recovery_ref,
                stage="late_recovery",
                input_refs=_step_input_refs(graph, late_recovery_ref, previous_ref),
                defect_before="propagated",
                defect_after="propagated",
            )
        )
        previous_ref = late_recovery_ref
    if observation_ref:
        steps.append(
            _evolution_step(
                graph,
                report,
                ref=observation_ref,
                stage="user_observation",
                input_refs=_step_input_refs(graph, observation_ref, previous_ref),
                defect_before=("propagated" if root_ref else "unknown"),
                defect_after="observed",
            )
        )

    return {
        "schema_version": DEFECT_EVOLUTION_SCHEMA_VERSION,
        "analysis_outcome": str(report.get("analysis_outcome") or "inconclusive"),
        "question": str(
            _mapping(report.get("analysis_question")).get("question") or ""
        ),
        "expected_behavior": str(premise.get("expected_behavior") or ""),
        "actual_behavior": str(premise.get("alleged_actual_behavior") or ""),
        "primary_cause": _primary_cause(root, root_ref),
        "steps": [
            {**step, "sequence": index}
            for index, step in enumerate(steps, start=1)
        ],
        "counterfactual": _counterfactual(root),
        "contributing_conditions": list(
            report.get("contributing_conditions") or ()
        ),
        "ruled_out": _ruled_out_components(report, graph),
        "evidence_gaps": list(report.get("unresolved_gaps") or ()),
        "generation": {
            "mode": "deterministic_causal_ir_projection",
            "analysis_mode": "offline_read_only",
        },
    }


def render_defect_explanation_markdown(
    report: Mapping[str, Any],
    evolution: Mapping[str, Any],
) -> str:
    lines = [
        "# 缺陷根因分析",
        "",
        "## 分析结论",
        "",
        str(
            _mapping(evolution.get("primary_cause")).get("explanation")
            or report.get("conclusion")
            or "当前证据不足以确认根因。"
        ),
        "",
        "## 用户期望与实际行为",
        "",
        "- 用户问题：{0}".format(evolution.get("question") or "未提供"),
        "- 期望行为：{0}".format(evolution.get("expected_behavior") or "未记录"),
        "- 实际行为：{0}".format(evolution.get("actual_behavior") or "未记录"),
        "",
        "## 缺陷产生过程",
        "",
    ]
    for step in evolution.get("steps") or ():
        if not isinstance(step, Mapping):
            continue
        stage = str(step.get("stage") or "")
        lines.extend(
            [
                "### 第 {0} 步：{1}".format(
                    step.get("sequence") or "?",
                    _STAGE_TITLES.get(stage, stage or "语义演化"),
                ),
                "",
                "- 节点：`{0}`".format(step.get("node_ref") or "unknown"),
                "- 组件：`{0}` / `{1}`".format(
                    step.get("component") or "unknown",
                    step.get("event_type") or "unknown",
                ),
                *(
                    ["- 步骤说明：{0}".format(step.get("explanation"))]
                    if step.get("explanation")
                    else []
                ),
                "- 输入语义：{0}".format(step.get("input_semantics") or "未记录"),
                "- 语义转换：{0}".format(step.get("transformation") or "未记录"),
                "- 输出语义：{0}".format(step.get("output_semantics") or "未记录"),
                "- 缺陷状态：`{0}` → `{1}`".format(
                    step.get("defect_before") or "unknown",
                    step.get("defect_after") or "unknown",
                ),
                "- 因果说明：{0}".format(step.get("causal_reason") or "未记录"),
                "- 原始证据：{0}".format(
                    _markdown_quote(str(step.get("evidence_excerpt") or "未记录"))
                ),
                "",
            ]
        )
    contributing = evolution.get("contributing_conditions") or ()
    lines.extend(["## 系统性诱因", ""])
    if contributing:
        for item in contributing:
            if not isinstance(item, Mapping):
                continue
            refs = ", ".join(
                "`{0}`".format(ref) for ref in item.get("evidence_refs") or ()
            )
            lines.append(
                "- **{0}**：{1}{2}".format(
                    item.get("title") or item.get("component") or "未分类诱因",
                    item.get("explanation") or item.get("reason") or "未记录",
                    "（证据：{0}）".format(refs) if refs else "",
                )
            )
    else:
        lines.append("- 当前证据未确认独立于直接根因的系统性诱因。")
    lines.extend(
        [
            "",
            "## 反事实",
            "",
            str(evolution.get("counterfactual") or "缺少可验证的反事实结论。"),
            "",
            "## 已排除原因",
            "",
        ]
    )
    ruled_out = evolution.get("ruled_out") or ()
    if ruled_out:
        for item in ruled_out:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                "- `{0}`：{1}".format(
                    item.get("component") or item.get("node_ref") or "unknown",
                    item.get("explanation")
                    or item.get("reason")
                    or "与当前缺陷无直接因果关系。",
                )
            )
    else:
        lines.append("- 当前报告未形成可审计的排除项。")
    if evolution.get("evidence_gaps"):
        lines.extend(["", "## 证据缺口", ""])
        for gap in evolution.get("evidence_gaps") or ():
            lines.append("- {0}".format(stable_json(gap)))
    recommendations = evolution.get("recommendations") or ()
    lines.extend(["", "## 改进建议", ""])
    if recommendations:
        for recommendation in recommendations:
            lines.append("- {0}".format(recommendation))
    else:
        lines.append("- 当前解释未生成有证据约束的改进建议。")
    return "\n".join(lines).rstrip() + "\n"


def synthesize_defect_evolution(
    evolution: Mapping[str, Any],
    *,
    transport: Any,
) -> dict[str, Any]:
    """Let an LLM explain grounded steps without letting it rewrite facts."""
    deterministic = _json_copy(evolution)
    steps = [item for item in deterministic.get("steps") or () if isinstance(item, Mapping)]
    if not steps:
        deterministic["generation"] = {
            "mode": "deterministic_fallback",
            "analysis_mode": "offline_read_only",
            "fallback_reason": "no grounded defect-evolution steps are available",
            "physical_requests": 0,
        }
        return deterministic

    allowed_ref_values = {
        ref
        for step in steps
        for ref in [step.get("node_ref"), *(step.get("evidence_refs") or ())]
        if isinstance(ref, str) and ref
    }
    primary_cause = _mapping(deterministic.get("primary_cause"))
    allowed_ref_values.update(
        str(ref)
        for ref in primary_cause.get("evidence_refs") or ()
        if str(ref)
    )
    for field in ("contributing_conditions", "ruled_out"):
        for item in _mapping_items(deterministic.get(field)):
            allowed_ref_values.update(
                str(ref) for ref in item.get("evidence_refs") or () if str(ref)
            )
    allowed_refs = frozenset(allowed_ref_values)
    prompt = stable_json(
        {
            "schema": DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION,
            "question": deterministic.get("question"),
            "expected_behavior": deterministic.get("expected_behavior"),
            "actual_behavior": deterministic.get("actual_behavior"),
            "primary_cause": deterministic.get("primary_cause"),
            "grounded_process_steps": steps,
            "grounded_ruled_out": deterministic.get("ruled_out") or [],
            "grounded_contributing_conditions": deterministic.get(
                "contributing_conditions"
            )
            or [],
            "instructions": [
                "Write a detailed Chinese root-cause explanation from the supplied facts only.",
                "Explain what entered each node, how its semantics changed, what it produced, and how the defect state changed.",
                "Preserve exactly the supplied process-step node refs and order; do not add, remove, merge, or reorder steps.",
                "Distinguish direct root, propagation/materialization, late recovery, observation, contributing conditions, and ruled-out causes.",
                "Every contributing or ruled-out statement must cite only an offered evidence ref.",
                "Return exactly one JSON object with no markdown.",
            ],
            "required_output": {
                "summary": "detailed Chinese summary",
                "primary_cause_explanation": "why the confirmed node is the root",
                "steps": [
                    {
                        "node_ref": "exact offered step ref",
                        "explanation": "step-level explanation",
                        "input_semantics": "what facts entered this node",
                        "transformation": "how this node interpreted or changed them",
                        "output_semantics": "what semantics or action left the node",
                        "causal_reason": "why this step has its recorded causal role",
                    }
                ],
                "contributing_conditions": [
                    {
                        "title": "short title",
                        "explanation": "bounded contributing condition",
                        "evidence_refs": ["offered ref"],
                    }
                ],
                "ruled_out": [
                    {
                        "component": "component name",
                        "explanation": "why it is not the direct root",
                        "evidence_refs": ["offered ref"],
                    }
                ],
                "counterfactual": "specific prevention counterfactual",
                "recommendations": ["actionable recommendation"],
            },
        }
    )
    system = (
        "You explain a confirmed causal attribution from an immutable semantic trace. "
        "You may improve prose but may not change causal facts, node identity, order, "
        "defect transitions, or evidence provenance."
    )
    messages = [{"role": "user", "content": prompt}]
    max_tokens = min(int(getattr(transport, "max_tokens", 4096)), 4096)
    cache_key = build_judge_cache_key(
        stage="defect_evolution_explanation",
        model=str(getattr(transport, "model", "unknown")),
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        thinking_config=getattr(transport, "thinking_config", None),
        prompt_schema_version=DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION,
    )
    cache = getattr(transport, "cache", None)

    def validate(value: Mapping[str, Any]) -> dict[str, Any]:
        return _validate_synthesis(
            value,
            expected_step_refs=[str(step.get("node_ref") or "") for step in steps],
            allowed_refs=allowed_refs,
        )

    try:
        payload = None
        cache_hit = False
        if isinstance(cache, JudgmentCache):
            payload = cache.get_validated_payload(key=cache_key, validator=validate)
            cache_hit = payload is not None
        if payload is None:
            payload = parse_json_object(
                transport.create_message_text(
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
            )
        synthesis = validate(payload)
        if isinstance(cache, JudgmentCache) and not cache_hit:
            cache.put_payload(
                key=cache_key,
                stage="defect_evolution_explanation",
                model=str(getattr(transport, "model", "unknown")),
                node_ref=str(
                    _mapping(deterministic.get("primary_cause")).get("node_ref")
                    or "analysis:defect_evolution"
                ),
                payload=synthesis,
            )
        return _merge_synthesis(
            deterministic,
            synthesis,
            physical_requests=0 if cache_hit else 1,
        )
    except Exception as exc:
        deterministic["generation"] = {
            "mode": "deterministic_fallback",
            "analysis_mode": "offline_read_only",
            "fallback_reason": "{0}: {1}".format(type(exc).__name__, exc),
            "physical_requests": 1,
        }
        return deterministic


def publish_defect_explanation(
    report_path: Path,
    report: Mapping[str, Any],
    evolution: Mapping[str, Any],
) -> Path:
    path = explanation_output_path(report_path)
    _atomic_write_text(path, render_defect_explanation_markdown(report, evolution))
    return path


def _validate_synthesis(
    value: Mapping[str, Any],
    *,
    expected_step_refs: list[str],
    allowed_refs: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("defect explanation must be an object")
    steps = _mapping_items(value.get("steps"))
    returned_refs = [str(step.get("node_ref") or "") for step in steps]
    if returned_refs != expected_step_refs:
        raise ValueError(
            "unsupported, missing, or reordered process node refs: {0}".format(
                returned_refs
            )
        )
    required_step_fields = (
        "explanation",
        "input_semantics",
        "transformation",
        "output_semantics",
        "causal_reason",
    )
    normalized_steps: list[dict[str, str]] = []
    for step in steps:
        normalized = {"node_ref": str(step.get("node_ref") or "")}
        for field in required_step_fields:
            text = str(step.get(field) or "").strip()
            if not text:
                raise ValueError(
                    "defect explanation step {0} lacks {1}".format(
                        normalized["node_ref"], field
                    )
                )
            normalized[field] = text
        normalized_steps.append(normalized)

    def grounded_items(field: str, label_field: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in _mapping_items(value.get(field)):
            evidence_refs = [
                str(ref) for ref in item.get("evidence_refs") or () if str(ref)
            ]
            if any(ref not in allowed_refs for ref in evidence_refs):
                raise ValueError("unsupported evidence ref in {0}".format(field))
            label = str(item.get(label_field) or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            if not label or not explanation or not evidence_refs:
                raise ValueError("incomplete grounded item in {0}".format(field))
            result.append(
                {
                    label_field: label,
                    "explanation": explanation,
                    "evidence_refs": list(dict.fromkeys(evidence_refs)),
                }
            )
        return result

    summary = str(value.get("summary") or "").strip()
    primary = str(value.get("primary_cause_explanation") or "").strip()
    counterfactual = str(value.get("counterfactual") or "").strip()
    if not summary or not primary or not counterfactual:
        raise ValueError("defect explanation summary, primary cause, and counterfactual are required")
    recommendations = [
        str(item).strip()
        for item in value.get("recommendations") or ()
        if str(item).strip()
    ]
    return {
        "summary": summary,
        "primary_cause_explanation": primary,
        "steps": normalized_steps,
        "contributing_conditions": grounded_items(
            "contributing_conditions", "title"
        ),
        "ruled_out": grounded_items("ruled_out", "component"),
        "counterfactual": counterfactual,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _merge_synthesis(
    evolution: dict[str, Any],
    synthesis: Mapping[str, Any],
    *,
    physical_requests: int,
) -> dict[str, Any]:
    explanations = {
        str(step.get("node_ref") or ""): step
        for step in _mapping_items(synthesis.get("steps"))
    }
    merged_steps: list[dict[str, Any]] = []
    for step in evolution.get("steps") or ():
        merged = dict(step)
        explanation = explanations[str(step.get("node_ref") or "")]
        for field in (
            "explanation",
            "input_semantics",
            "transformation",
            "output_semantics",
            "causal_reason",
        ):
            merged[field] = explanation[field]
        merged_steps.append(merged)
    evolution["steps"] = merged_steps
    evolution["summary"] = synthesis["summary"]
    primary = dict(_mapping(evolution.get("primary_cause")))
    primary["explanation"] = synthesis["primary_cause_explanation"]
    evolution["primary_cause"] = primary
    evolution["contributing_conditions"] = list(
        synthesis.get("contributing_conditions") or ()
    )
    evolution["ruled_out"] = list(synthesis.get("ruled_out") or ())
    evolution["counterfactual"] = synthesis["counterfactual"]
    evolution["recommendations"] = list(synthesis.get("recommendations") or ())
    evolution["generation"] = {
        "mode": "llm_grounded_synthesis",
        "analysis_mode": "offline_read_only",
        "prompt_schema_version": DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION,
        "physical_requests": physical_requests,
    }
    return evolution


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _evolution_step(
    graph: TraceGraph,
    report: Mapping[str, Any],
    *,
    ref: str,
    stage: str,
    input_refs: list[str],
    defect_before: str,
    defect_after: str,
) -> dict[str, Any]:
    node = graph.nodes.get(ref)
    reason = _judgment_reason(report, ref)
    if node is None and stage != "user_observation":
        raise ValueError("defect evolution ref is missing from graph: {0}".format(ref))
    data = (
        node.data
        if node is not None
        else {
            "question": _mapping(report.get("analysis_question")).get("question"),
            "expected": _question_premise(report).get("expected_behavior"),
            "actual": _question_premise(report).get("alleged_actual_behavior"),
        }
    )
    component = node.component if node is not None else "user"
    event_type = node.event_type if node is not None else "case.observed_defect"
    excerpt = _semantic_excerpt(data)
    return {
        "sequence": 0,
        "stage": stage,
        "node_ref": ref,
        "component": _semantic_component(component, event_type),
        "runtime_component": component,
        "event_type": event_type,
        "input_refs": input_refs,
        "input_semantics": _input_semantics(stage, input_refs, report),
        "transformation": _transformation(stage, reason, excerpt),
        "output_semantics": _output_semantics(stage, data, excerpt),
        "defect_before": defect_before,
        "defect_after": defect_after,
        "causal_relation": _stage_relation(stage),
        "causal_reason": reason or _default_causal_reason(stage),
        "evidence_excerpt": excerpt,
        "evidence_refs": list(dict.fromkeys([ref, *input_refs])),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value or () if isinstance(item, Mapping)]


def _question_premise(report: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(report.get("analysis_question")).get("premise_assessment"))


def _grounded_ref(graph: TraceGraph, value: Any) -> str:
    ref = str(value or "")
    resolved = graph.resolve(ref) or ref
    return resolved if resolved in graph.nodes else ""


def _grounded_refs(graph: TraceGraph, values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values or ():
        ref = _grounded_ref(graph, value)
        if ref and ref not in result:
            result.append(ref)
    return result


def _root_causal_path(
    report: Mapping[str, Any],
    graph: TraceGraph,
    root: Mapping[str, Any],
    root_ref: str,
) -> list[str]:
    candidates = [root.get("recursive_path")]
    candidates.extend(report.get("causal_chain") or ())
    for candidate in candidates:
        refs = _causal_path_refs(graph, candidate or ())
        if root_ref and root_ref in refs:
            return refs[refs.index(root_ref) :]
    return [root_ref] if root_ref else []


def _causal_path_refs(graph: TraceGraph, values: Iterable[Any]) -> list[str]:
    refs: list[str] = []
    for value in values:
        raw_ref = str(value or "")
        resolved = graph.resolve(raw_ref) or raw_ref
        if resolved in graph.nodes:
            ref = resolved
        elif raw_ref.startswith("record:offline_question_"):
            ref = raw_ref
        else:
            continue
        if ref not in refs:
            refs.append(ref)
    return refs


def _root_context_refs(graph: TraceGraph, root_ref: str) -> list[str]:
    if not root_ref or root_ref not in graph.nodes:
        return []
    data = graph.nodes[root_ref].data
    result: list[str] = []
    for transform in _mapping_items(data.get("message_transforms")):
        ref = _grounded_ref(graph, transform.get("node_ref"))
        if ref and ref not in result:
            result.append(ref)
    return result


def _contract_next_actions(graph: TraceGraph, refs: Iterable[str]) -> set[str]:
    actions: set[str] = set()
    for ref in refs:
        _collect_next_actions(graph.nodes[ref].data, actions)
    return actions


def _collect_next_actions(value: Any, actions: set[str]) -> None:
    if isinstance(value, Mapping):
        next_tool = value.get("next_tool")
        if isinstance(next_tool, str) and next_tool.strip():
            actions.add(next_tool.strip())
        for child in value.values():
            _collect_next_actions(child, actions)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _collect_next_actions(child, actions)
        return
    if not isinstance(value, str):
        return
    text = value.strip()
    if not text or text[0] not in "[{":
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return
    _collect_next_actions(parsed, actions)


def _late_recovery_ref(
    graph: TraceGraph,
    actual_sequence: list[str],
    *,
    path_body: list[str],
    observation_ref: str,
    next_actions: set[str],
    excluded_refs: frozenset[str],
) -> str:
    if not next_actions or not actual_sequence:
        return ""
    start_ref = path_body[-1] if path_body else ""
    start = actual_sequence.index(start_ref) + 1 if start_ref in actual_sequence else 0
    end = (
        actual_sequence.index(observation_ref)
        if observation_ref in actual_sequence
        else len(actual_sequence)
    )
    for ref in actual_sequence[start:end]:
        if ref in excluded_refs:
            continue
        node_text = stable_json(graph.nodes[ref].data)
        if any(action in node_text for action in next_actions):
            return ref
    return ""


def _step_input_refs(graph: TraceGraph, ref: str, previous_ref: str) -> list[str]:
    refs = (
        _grounded_refs(graph, graph.upstream_refs(ref))
        if ref in graph.nodes
        else []
    )
    if previous_ref and previous_ref not in refs:
        refs.append(previous_ref)
    return refs


def _semantic_excerpt(data: Mapping[str, Any], *, limit: int = 1200) -> str:
    rationale = data.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        return rationale.strip()[:limit]
    if isinstance(rationale, Mapping):
        for key in ("recent_reasoning", "reasoning", "preview"):
            value = rationale.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    output = data.get("output")
    if isinstance(output, Mapping):
        preview = output.get("preview")
        if isinstance(preview, str) and preview.strip():
            return preview.strip()[:limit]
    for key in ("text", "summary", "actual", "expected", "chosen_action", "tool_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    return stable_json(data)[:limit]


def _judgment_reason(report: Mapping[str, Any], ref: str) -> str:
    for judgment in _mapping_items(report.get("step_judgments")):
        if str(judgment.get("current_node_ref") or "") == ref:
            return str(judgment.get("current_defect_reason") or "")
    for root in _mapping_items(report.get("confirmed_roots")):
        if str(root.get("node_ref") or "") == ref:
            return str(root.get("reason") or "")
    return ""


def _semantic_component(component: str, event_type: str) -> str:
    if component == "processor" and event_type == "decision":
        return "agent.decision_generation"
    if event_type in {"tool.call", "mcp.call", "skill.call"}:
        return "agent.action_execution"
    if event_type == "case.observed_defect":
        return "user.evaluation"
    return component or "unknown"


def _input_semantics(
    stage: str,
    input_refs: list[str],
    report: Mapping[str, Any],
) -> str:
    premise = _question_premise(report)
    if stage == "contract_established":
        return "用户要求与工具结果共同形成后续行为契约。"
    if stage == "context_delivery":
        return "接收前序契约或上下文投影：{0}。".format(
            ", ".join(input_refs) or "未记录"
        )
    if stage == "defect_introduction":
        return "节点已接收行为契约及其上游事实：{0}。".format(
            ", ".join(input_refs) or "未记录"
        )
    if stage == "user_observation":
        return str(premise.get("alleged_actual_behavior") or "用户观察到执行偏差。")
    return "节点接收前序语义和执行结果：{0}。".format(
        ", ".join(input_refs) or "未记录"
    )


def _transformation(stage: str, reason: str, excerpt: str) -> str:
    if reason:
        return reason
    if stage == "contract_established":
        return "将工具输出中的后续动作要求记录为行为契约。"
    if stage == "context_delivery":
        return "将前序语义转换为模型消息、请求参数或 Provider 输入，本阶段未引入缺陷。"
    if stage == "defect_materialization":
        return "将前序缺陷决策转换为实际动作。"
    if stage == "late_recovery":
        return "补做先前要求的动作，但已经发生的顺序偏差不可逆。"
    if stage == "user_observation":
        return "将已发生的执行过程与用户期望进行比较。"
    return excerpt or "未记录可解释的语义转换。"


def _output_semantics(stage: str, data: Mapping[str, Any], excerpt: str) -> str:
    if stage == "contract_established":
        return "产生可供后续决策消费的行为契约：{0}".format(excerpt)
    if stage == "context_delivery":
        return "形成供模型决策使用的上下文投影：{0}".format(excerpt)
    if stage == "defect_introduction":
        return "产生首次违背行为契约的决策：{0}".format(excerpt)
    if stage == "defect_materialization":
        return "执行动作 `{0}`，使错误决策成为可观测行为。".format(
            data.get("chosen_action") or data.get("tool_name") or "unknown"
        )
    if stage == "late_recovery":
        return "执行迟到的动作 `{0}`。".format(
            data.get("chosen_action") or data.get("tool_name") or "unknown"
        )
    if stage == "user_observation":
        return "形成用户提出的缺陷质疑。"
    return excerpt


def _stage_relation(stage: str) -> str:
    return {
        "contract_established": "non_defective_input",
        "context_delivery": "non_defective_provenance",
        "defect_introduction": "defect_introduction",
        "defect_materialization": "same_defect_propagation",
        "defect_propagation": "same_defect_propagation",
        "late_recovery": "non_repairing_followup",
        "user_observation": "outcome_evidence",
    }.get(stage, "unknown")


def _default_causal_reason(stage: str) -> str:
    return {
        "contract_established": "该节点定义期望行为，本身不包含缺陷。",
        "context_delivery": "该节点传递或转换正确输入，没有改变其关键语义。",
        "defect_introduction": "该节点首次把正确输入转换为缺陷语义。",
        "defect_materialization": "该节点落实前序缺陷，但不是首次来源。",
        "defect_propagation": "该节点继续携带既有缺陷。",
        "late_recovery": "该动作发生在偏差之后，不能撤销既有顺序错误。",
        "user_observation": "该节点记录缺陷结果，不负责引入缺陷。",
    }.get(stage, "当前因果关系未确定。")


def _primary_cause(root: Mapping[str, Any], root_ref: str) -> dict[str, Any]:
    return {
        "node_ref": root_ref,
        "component": _semantic_component(
            str(root.get("component") or ""),
            str(root.get("event_type") or ""),
        ),
        "runtime_component": str(root.get("component") or ""),
        "defect_type": str(root.get("defect_type") or ""),
        "explanation": str(root.get("reason") or ""),
        "confidence": root.get("confidence", 0.0),
        "evidence_refs": list(root.get("evidence_refs") or ()),
    }


def _counterfactual(root: Mapping[str, Any]) -> str:
    confirmation = _mapping(root.get("confirmation"))
    counterfactual = root.get("counterfactual") or confirmation.get("counterfactual")
    if isinstance(counterfactual, str) and counterfactual.strip():
        try:
            value = json.loads(counterfactual)
        except json.JSONDecodeError:
            return counterfactual.strip()
        if isinstance(value, Mapping):
            return (
                "若在 `{0}` 用符合契约的行为替换当前决策，预测缺陷状态将变为 `{1}`。"
            ).format(
                value.get("intervention_ref") or root.get("node_ref") or "unknown",
                value.get("predicted_defect_status") or "absent",
            )
    node_ref = str(root.get("node_ref") or "unknown")
    return "若 `{0}` 不引入该错误语义，后续动作将不会传播同一缺陷。".format(
        node_ref
    )


def _ruled_out_components(
    report: Mapping[str, Any], graph: TraceGraph
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for judgment in _mapping_items(report.get("step_judgments")):
        for predecessor in _mapping_items(judgment.get("predecessors")):
            relation = str(predecessor.get("relation") or "")
            if relation not in {"unrelated", "motivated_by_evidence"}:
                continue
            ref = _grounded_ref(graph, predecessor.get("ref"))
            if not ref:
                continue
            node = graph.nodes[ref]
            component = _semantic_component(node.component, node.event_type)
            key = "{0}:{1}".format(component, ref)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "node_ref": ref,
                    "component": component,
                    "relation": relation,
                    "reason": str(predecessor.get("reason") or ""),
                    "evidence_refs": list(predecessor.get("evidence_refs") or (ref,)),
                }
            )
    return result


def _markdown_quote(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) > 360:
        compact = compact[:357] + "..."
    return "`{0}`".format(compact.replace("`", "\\`"))


__all__ = [
    "DEFECT_EVOLUTION_SCHEMA_VERSION",
    "build_defect_evolution",
    "explanation_output_path",
    "publish_defect_explanation",
    "render_defect_explanation_markdown",
    "synthesize_defect_evolution",
]
