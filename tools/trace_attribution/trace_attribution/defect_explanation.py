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


DEFECT_EVOLUTION_SCHEMA_VERSION = "defect-evolution/v2"
DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION = "defect-explanation-prompt/v2"


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
    expected_sequence, actual_action_sequence = _behavior_sequences(
        graph,
        report,
        contract_refs=contract_refs,
        actual_sequence=actual_sequence,
        next_actions=next_actions,
    )
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

    first_deviation = next(
        (step for step in steps if step.get("stage") == "defect_introduction"),
        {},
    )
    return {
        "schema_version": DEFECT_EVOLUTION_SCHEMA_VERSION,
        "analysis_outcome": str(report.get("analysis_outcome") or "inconclusive"),
        "question": str(
            _mapping(report.get("analysis_question")).get("question") or ""
        ),
        "expected_behavior": str(premise.get("expected_behavior") or ""),
        "actual_behavior": str(premise.get("alleged_actual_behavior") or ""),
        "defect_subject": _defect_subject(
            expected_sequence,
            actual_action_sequence,
            expected_behavior=str(premise.get("expected_behavior") or ""),
            actual_behavior=str(premise.get("alleged_actual_behavior") or ""),
        ),
        "expected_sequence": expected_sequence,
        "actual_sequence": actual_action_sequence,
        "first_deviation": {
            "node_ref": str(first_deviation.get("node_ref") or ""),
            "actor": str(first_deviation.get("actor") or ""),
            "action": str(first_deviation.get("action_description") or ""),
            "explanation": str(first_deviation.get("problem_explanation") or ""),
        },
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
        "## 一句话结论",
        "",
        _human_conclusion(evolution, report),
        "",
        "- 本次追踪的偏差：{0}".format(
            evolution.get("defect_subject") or "当前证据不足以明确命名偏差。"
        ),
        "",
        "## 期望动作与实际动作",
        "",
        "- 用户关心的问题：{0}".format(evolution.get("question") or "未提供"),
        "- 期望顺序：{0}".format(
            _render_action_sequence(evolution.get("expected_sequence") or ())
            or evolution.get("expected_behavior")
            or "未记录"
        ),
        "- 实际顺序：{0}".format(
            _render_action_sequence(evolution.get("actual_sequence") or ())
            or evolution.get("actual_behavior")
            or "未记录"
        ),
        "- 首次偏离：{0}".format(
            _mapping(evolution.get("first_deviation")).get("action")
            or "当前证据不足以定位"
        ),
        "",
        "## 缺陷是怎样一步步产生的",
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
                    step.get("human_title")
                    or _STAGE_TITLES.get(stage, stage or "语义演化"),
                ),
                "",
                "- **谁在做什么**：{0}。{1}".format(
                    step.get("actor") or "未识别的组件",
                    step.get("action_description") or "未记录具体动作。",
                ),
                "- **当时掌握的信息**：{0}".format(
                    step.get("knowledge_at_time") or "未记录"
                ),
                "- **为什么这一步有问题**：{0}".format(
                    step.get("problem_explanation") or "本步骤没有引入新的偏差。"
                ),
                "- **对下一步的影响**：{0}".format(
                    step.get("effect_on_next") or "未记录"
                ),
                "- **此时偏差发展到哪里**：{0}".format(
                    step.get("human_defect_state") or "未确定"
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
            lines.append(
                "- **{0}**：{1}".format(
                    item.get("title") or item.get("component") or "未分类诱因",
                    item.get("explanation") or item.get("reason") or "未记录",
                )
            )
    else:
        lines.append("- 当前证据未确认独立于直接根因的系统性诱因。")
    lines.extend(
        [
            "",
            "## 反事实",
            "",
            _human_counterfactual(evolution),
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
                "- **{0}**：{1}".format(
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
    lines.extend(["", "## 技术证据附录", ""])
    lines.append(
        "以下内容用于审计归因结论；普通读者不需要理解节点 ID、事件类型或内部状态码。"
    )
    lines.append("")
    for step in evolution.get("steps") or ():
        if not isinstance(step, Mapping):
            continue
        lines.extend(
            [
                "### 证据 {0}：`{1}`".format(
                    step.get("sequence") or "?", step.get("node_ref") or "unknown"
                ),
                "",
                "- 运行时组件/事件：`{0}` / `{1}`".format(
                    step.get("runtime_component") or "unknown",
                    step.get("event_type") or "unknown",
                ),
                "- 内部缺陷状态：`{0}` -> `{1}`".format(
                    step.get("defect_before") or "unknown",
                    step.get("defect_after") or "unknown",
                ),
                "- 输入节点：{0}".format(
                    ", ".join(
                        "`{0}`".format(ref) for ref in step.get("input_refs") or ()
                    )
                    or "无"
                ),
                "- 原始证据：{0}".format(
                    _markdown_quote(str(step.get("evidence_excerpt") or "未记录"))
                ),
                "",
            ]
        )
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
                "Write the human-facing fields as an engineering incident narrative, not as a translation of trace fields.",
                "For every step, explicitly say who did what, what information was available then, why the step was or was not defective, and how it affected the next step.",
                "Do not use unexplained event names such as tool.result, component identifiers, or state codes such as absent/present in human-facing fields.",
                "Use concrete nouns: name the tool, Skill, action, requirement, decision, file, or result being discussed whenever the supplied facts contain it.",
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
                        "human_title": "short Chinese title describing the concrete event",
                        "actor": "human-readable actor or subsystem name",
                        "action_description": "what this actor concretely did",
                        "knowledge_at_time": "what relevant requirement and evidence were available at that moment",
                        "problem_explanation": "why this step introduced, propagated, repaired, observed, or did not cause the deviation",
                        "effect_on_next": "how this output concretely changed the next decision or action",
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
        "human_title",
        "actor",
        "action_description",
        "knowledge_at_time",
        "problem_explanation",
        "effect_on_next",
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
            "human_title",
            "actor",
            "action_description",
            "knowledge_at_time",
            "problem_explanation",
            "effect_on_next",
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
    human = _human_step_fields(
        graph,
        report,
        ref=ref,
        stage=stage,
        data=data,
        excerpt=excerpt,
    )
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
        **human,
    }


def _behavior_sequences(
    graph: TraceGraph,
    report: Mapping[str, Any],
    *,
    contract_refs: list[str],
    actual_sequence: list[str],
    next_actions: set[str],
) -> tuple[list[str], list[str]]:
    ordered_contract_actions: list[str] = []
    for ref in contract_refs:
        actions: set[str] = set()
        _collect_next_actions(graph.nodes[ref].data, actions)
        for action in sorted(actions):
            label = "调用 {0}".format(action)
            if label not in ordered_contract_actions:
                ordered_contract_actions.append(label)
    for action in sorted(next_actions):
        label = "调用 {0}".format(action)
        if label not in ordered_contract_actions:
            ordered_contract_actions.append(label)

    premise = _question_premise(report)
    if str(premise.get("deviation_type") or "") == "action_order":
        contract_positions = [
            actual_sequence.index(ref)
            for ref in contract_refs
            if ref in actual_sequence
        ]
        window_start = max(contract_positions) + 1 if contract_positions else 0
        first_deviation_ref = _grounded_ref(
            graph, premise.get("first_deviation_ref")
        )
        first_deviation_position = (
            actual_sequence.index(first_deviation_ref)
            if first_deviation_ref in actual_sequence
            else window_start
        )
        relevant_actual: list[str] = []
        deviation_action_found = False
        for index, ref in enumerate(actual_sequence[window_start:], start=window_start):
            node = graph.nodes.get(ref)
            if node is None:
                continue
            label = _node_action_label(node.data, node.event_type)
            if not label:
                continue
            required_label = _matching_contract_action(
                label, ordered_contract_actions
            )
            is_deviation_action = ref == first_deviation_ref
            if (
                not is_deviation_action
                and not deviation_action_found
                and index >= first_deviation_position
                and not required_label
            ):
                is_deviation_action = True
            if not is_deviation_action and not required_label:
                continue
            normalized = required_label or label
            if not relevant_actual or relevant_actual[-1] != normalized:
                relevant_actual.append(normalized)
            deviation_action_found = deviation_action_found or is_deviation_action

        expected = list(ordered_contract_actions)
        expected.extend(
            action
            for action in relevant_actual
            if action not in expected
        )
        return expected, relevant_actual

    actual_actions: list[str] = []
    for ref in actual_sequence:
        node = graph.nodes.get(ref)
        if node is None:
            continue
        label = _node_action_label(node.data, node.event_type)
        if label and (not actual_actions or actual_actions[-1] != label):
            actual_actions.append(label)
    return ordered_contract_actions, actual_actions


def _matching_contract_action(
    actual_label: str, contract_actions: Iterable[str]
) -> str:
    if not actual_label.startswith("调用 "):
        return ""
    actual_name = _normalized_action_name(actual_label[len("调用 ") :])
    for contract_label in contract_actions:
        if not contract_label.startswith("调用 "):
            continue
        contract_name = _normalized_action_name(contract_label[len("调用 ") :])
        if actual_name == contract_name or actual_name.endswith("_" + contract_name):
            return contract_label
    return ""


def _normalized_action_name(value: str) -> str:
    return value.replace(":", "_").replace(".", "_").strip().lower()


def _node_action_label(data: Mapping[str, Any], event_type: str) -> str:
    action = str(data.get("chosen_action") or data.get("tool_name") or "").strip()
    if not action:
        return ""
    if action in {"edit", "write", "apply_patch"}:
        return "修改代码"
    if event_type in {"tool.call", "mcp.call", "skill.call"}:
        return "调用 {0}".format(action)
    if str(data.get("decision_type") or "") == "llm_tool_call":
        return "修改代码" if action in {"edit", "write", "apply_patch"} else "调用 {0}".format(action)
    return ""


def _defect_subject(
    expected_sequence: list[str],
    actual_sequence: list[str],
    *,
    expected_behavior: str,
    actual_behavior: str,
) -> str:
    if len(expected_sequence) >= 2 and len(actual_sequence) >= 2:
        return "本应先{0} 再{1}，但实际先{2}、后{3}。".format(
            _subject_action(expected_sequence[0]),
            _subject_action(expected_sequence[1]),
            _subject_action(actual_sequence[0]),
            _subject_action(actual_sequence[1]),
        )
    if expected_sequence and actual_sequence:
        return "期望执行“{0}”，但实际执行了“{1}”。".format(
            " -> ".join(expected_sequence), " -> ".join(actual_sequence)
        )
    if expected_behavior or actual_behavior:
        return "期望“{0}”，但实际“{1}”。".format(
            expected_behavior or "未记录", actual_behavior or "未记录"
        )
    return "当前证据不足以明确命名偏差。"


def _subject_action(action: str) -> str:
    return "编辑" if action == "修改代码" else action


def _render_action_sequence(actions: Iterable[Any]) -> str:
    rendered = [_render_action(str(action)) for action in actions if str(action)]
    if not rendered:
        return ""
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return "先{0}，再{1}".format(rendered[0], rendered[1])
    return "先{0}，然后{1}，最后{2}".format(
        rendered[0], "、".join(rendered[1:-1]), rendered[-1]
    )


def _render_action(action: str) -> str:
    if action.startswith("调用 "):
        return "调用 `{0}`".format(action[len("调用 ") :])
    return action


def _human_conclusion(
    evolution: Mapping[str, Any], report: Mapping[str, Any]
) -> str:
    generation = _mapping(evolution.get("generation"))
    summary = str(evolution.get("summary") or "").strip()
    if generation.get("mode") == "llm_grounded_synthesis" and summary:
        return summary
    first = _mapping(evolution.get("first_deviation"))
    actor = str(first.get("actor") or "相关组件").strip()
    action = str(first.get("action") or "首次作出了偏离期望的决定").strip()
    action = action.rstrip("。；; ")
    if first:
        expected = _render_action_sequence(
            evolution.get("expected_sequence") or ()
        ) or "既定要求"
        return "{0}{1}。这一决定首次打破了“{2}”的执行要求，并在下一步被落实为实际动作。".format(
            actor, action, expected
        )
    return str(
        _mapping(evolution.get("primary_cause")).get("explanation")
        or report.get("conclusion")
        or "当前证据不足以确认根因。"
    )


def _human_counterfactual(evolution: Mapping[str, Any]) -> str:
    expected = _render_action_sequence(evolution.get("expected_sequence") or ())
    if expected:
        return "如果 Agent 在首次决策时遵循期望顺序（{0}），后续就不会把该顺序偏差落实为实际动作。".format(
            expected
        )
    return "如果首次偏离节点不覆盖已经收到的正确要求，后续就不会继续传递同一偏差。"


def _human_step_fields(
    graph: TraceGraph,
    report: Mapping[str, Any],
    *,
    ref: str,
    stage: str,
    data: Mapping[str, Any],
    excerpt: str,
) -> dict[str, str]:
    premise = _question_premise(report)
    action = _node_action_label(data, str(graph.nodes[ref].event_type)) if ref in graph.nodes else ""
    expected = str(premise.get("expected_behavior") or "既定行为要求").strip()
    actual = str(
        premise.get("alleged_actual_behavior") or excerpt or "记录到的实际行为"
    ).strip()
    is_action_order = str(premise.get("deviation_type") or "") == "action_order"

    if stage == "contract_established":
        return {
            "human_title": (
                "执行顺序要求被明确记录"
                if is_action_order
                else "行为要求被明确记录"
            ),
            "actor": _human_actor(data, ref, graph),
            "action_description": "记录了后续行为应满足的要求：{0}".format(expected),
            "knowledge_at_time": "该节点掌握了形成行为约束所需的输入信息。",
            "problem_explanation": "本步骤建立了判断偏差的基准，没有证据表明它引入了缺陷。",
            "effect_on_next": "该要求成为后续决策和动作应当遵循的约束。",
            "human_defect_state": "行为要求已经形成，尚未观察到偏差。",
        }
    if stage == "context_delivery":
        return {
            "human_title": "行为要求进入后续处理上下文",
            "actor": "上下文管理层",
            "action_description": "向后续节点传递行为要求：{0}".format(expected),
            "knowledge_at_time": "上下文中保留了用于判断后续行为是否符合预期的信息。",
            "problem_explanation": "当前证据未显示本步骤删除或篡改了关键要求。",
            "effect_on_next": "后续决策可以依据该要求选择行为。",
            "human_defect_state": "要求仍然可用，偏差尚未在此处引入。",
        }
    if stage == "defect_introduction":
        first_actual = _first_deviation_action(graph, report)
        action_description = action or excerpt or actual
        if is_action_order and first_actual:
            action_description = "决定先{0}；记录到的实际顺序是：{1}".format(
                first_actual,
                actual,
            )
        return {
            "human_title": (
                "Agent 首次改变了执行顺序"
                if is_action_order
                else "偏差在本节点首次出现"
            ),
            "actor": _human_actor(data, ref, graph),
            "action_description": action_description,
            "knowledge_at_time": "可用要求是：{0}".format(expected),
            "problem_explanation": "本节点首次产生了与要求不一致的语义或决定：{0}".format(actual),
            "effect_on_next": "这一输出把记录到的偏差带入后续步骤：{0}".format(
                actual
            ),
            "human_defect_state": (
                "执行要求原本正确，但在本步骤首次产生了顺序偏差。"
                if is_action_order
                else "输入中的要求仍然正确，但本节点首次产生了偏差。"
            ),
        }
    if stage == "defect_materialization":
        return {
            "human_title": "偏差被落实为实际行为",
            "actor": _human_actor(data, ref, graph),
            "action_description": action or excerpt or actual,
            "knowledge_at_time": "接收到的上游语义已经包含偏差。",
            "problem_explanation": "本节点不是最早的根因，但把上游偏差转化成了可观察行为。",
            "effect_on_next": "后续处理将建立在这一偏离预期的实际结果上：{0}".format(actual),
            "human_defect_state": "偏差从内部决定转化为外部可观察结果。",
        }
    if stage == "defect_propagation":
        return {
            "human_title": "已有偏差继续影响后续处理",
            "actor": _human_actor(data, ref, graph),
            "action_description": "继续处理已经带有偏差的前序结果。",
            "knowledge_at_time": "接收到的上游执行过程已经偏离期望。",
            "problem_explanation": "本步骤没有首次制造偏差，但继续携带了它。",
            "effect_on_next": "后续结果继续建立在已经偏离的执行过程上。",
            "human_defect_state": "已有偏差继续向后传递。",
        }
    if stage == "late_recovery":
        return {
            "human_title": "后续步骤尝试补偿已有偏差",
            "actor": _human_actor(data, ref, graph),
            "action_description": action or excerpt or "执行了补偿动作。",
            "knowledge_at_time": "此前流程已经产生了与要求不一致的结果。",
            "problem_explanation": "本步骤可能减轻影响，但不能证明最早的偏差从未发生。",
            "effect_on_next": "补偿结果进入后续处理，同时保留原始偏差的因果记录。",
            "human_defect_state": "影响可能得到部分修复，原始偏差仍可追溯。",
        }
    if stage == "user_observation":
        return {
            "human_title": "用户发现最终行为不符合预期",
            "actor": "用户",
            "action_description": "比较预期执行过程与实际执行记录，并提出质疑。",
            "knowledge_at_time": "期望是“{0}”，实际是“{1}”。".format(
                premise.get("expected_behavior") or "未记录",
                premise.get("alleged_actual_behavior") or "未记录",
            ),
            "problem_explanation": "这里记录的是偏差被发现，而不是偏差被制造。",
            "effect_on_next": "该观察成为离线归因分析的起点。",
            "human_defect_state": "先前产生并传递的偏差最终被用户观察到。",
        }
    return {
        "human_title": _STAGE_TITLES.get(stage, "语义处理"),
        "actor": _human_actor(data, ref, graph),
        "action_description": excerpt or "执行了本阶段处理。",
        "knowledge_at_time": "接收到前序节点提供的信息。",
        "problem_explanation": "当前证据不足以说明本步骤是否引入新偏差。",
        "effect_on_next": "处理结果被传递给后续节点。",
        "human_defect_state": "偏差状态尚不明确。",
    }


def _first_deviation_action(graph: TraceGraph, report: Mapping[str, Any]) -> str:
    premise = _question_premise(report)
    first_deviation_ref = _grounded_ref(graph, premise.get("first_deviation_ref"))
    if first_deviation_ref:
        node = graph.nodes[first_deviation_ref]
        label = _node_action_label(node.data, node.event_type)
        if label:
            return label
    actual_refs = _grounded_refs(graph, premise.get("actual_sequence_refs"))
    start = (
        actual_refs.index(first_deviation_ref)
        if first_deviation_ref in actual_refs
        else 0
    )
    for ref in actual_refs[start:]:
        node = graph.nodes[ref]
        label = _node_action_label(node.data, node.event_type)
        if label:
            return label
    return ""


def _human_actor(data: Mapping[str, Any], ref: str, graph: TraceGraph) -> str:
    node = graph.nodes.get(ref)
    if node is None:
        return "未识别的组件"
    tool_name = str(data.get("tool_name") or data.get("chosen_action") or "").strip()
    if tool_name:
        return "`{0}` 工具执行层".format(tool_name)
    if node.component == "processor":
        return "Agent 决策层"
    if node.component == "context":
        return "上下文管理层"
    return "{0} 组件".format(node.component or "未识别")


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
