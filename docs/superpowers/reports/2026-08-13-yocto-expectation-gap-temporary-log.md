# Yocto 构建路径偏差归因临时问题日志

日期：2026-08-13
状态：问题已确认，临时规避方案已记录，正式能力尚未实施

## 1. 问题背景

某项目默认使用 Yocto 进行编译构建。用户新增代码后，明确要求 Agent 使用一个编译构建
Skill 完成编译验证，但 Agent 没有执行项目自带的 Yocto/BitBake 构建流程，而是自行调用
GCC 完成了局部编译。

用户将该过程的语义 Trace 交给离线归因模块，并提出以下问题：

> 项目默认使用 Yocto 构建，用户要求使用构建 Skill 完成编译验证，但实际只执行了 GCC
> 局部编译。请判断 Yocto 要求是否进入模型上下文、Skill 是否可用并被加载、Skill 是否
> 明确要求 Yocto、首次偏离预期的决策节点是什么，以及该偏差属于 Skill 设计、技能选择、
> 上下文丢失、Agent 决策还是合理降级。

归因结果仍然没有确认根因，主要输出为：

```text
missing_semantic_final_test_result
observed_defect_missing_verification_after_change
evidence_insufficient
```

## 2. 当前判断

该结果不能解释为“Agent 没有问题”，也不能解释为“Yocto 与 GCC 等价”。它说明当前归因
模块实际分析的是 Trace 自动生成的“代码修改后缺少最终测试结果”，并没有把用户问题中的
“Yocto 预期路径与 GCC 实际路径不一致”构造成可后向遍历的分析目标。

GCC 返回成功最多能够证明特定源码或翻译单元完成了局部编译。它通常不能替代 Yocto 工程
级验证所覆盖的以下内容：

- BitBake task、recipe、layer 和配置；
- 交叉编译工具链、目标架构和 sysroot；
- Yocto 注入的编译参数、依赖和特性开关；
- 打包、镜像集成及目标系统兼容性。

因此，即使 GCC 退出码为 0，仍可能存在两类可分析问题：

1. **执行路径偏差**：没有遵循用户或项目规定的权威构建流程；
2. **验证覆盖不足**：将局部编译结果当成工程级构建验证结果。

## 3. 已确认的实现原因

### 3.1 `--question` 不会创建新的分析种子

当前 `--question` 会作为归因 Objective 传给 Judge，并用于重新排序已有默认起点，但不会
根据问题文本生成新的 `expectation_gap`、`task.obligation` 或 `case.quality_gap` 节点。

相关实现：

- `tools/trace_attribution/trace_attribution/service.py::analysis_start_refs`
- `tools/trace_attribution/trace_attribution/service.py::question_bound_output_payload`

`analysis_start_refs` 对问题做词元匹配后，只重新排序 `graph.default_start_refs()` 返回的全部
默认起点。它不会过滤无关起点，也不会把“期望使用 Yocto、实际使用 GCC”投影为新的起点。

### 3.2 默认起点被自动 Trace 健康问题占据

当 Trace 记录了代码变更，但没有记录被当前分类器识别为 `verification` 的命令时，运行时会
生成：

- `case.missing_semantic: missing_semantic_final_test_result`
- `case.observed_defect: observed_defect_missing_verification_after_change`

相关实现：

- `packages/opencode/src/observability/case-trace.ts::caseDiagnosticNodes`
- `tools/trace_attribution/trace_attribution/graph.py::default_start_refs`

这些节点随后成为默认归因起点，因此分析围绕“没有最终测试结果”展开，而不是围绕 Yocto
执行路径偏差展开。

### 3.3 构建命令分类覆盖不足

当前 shell 命令分类主要识别 pytest、npm test、cargo test、xcodebuild 等测试命令，没有
完整识别以下构建与编译验证入口：

- `gcc` / `clang`；
- `make` / `cmake` / `ninja`；
- `bitbake`；
- `kas build`；
- Yocto 环境初始化及其后续构建命令。

相关实现：

- `packages/opencode/src/tool/tool.ts::isVerificationCommand`
- `packages/opencode/src/tool/tool.ts::classifyShellOperation`
- `packages/opencode/src/observability/case-trace.ts::isTestLikeCommand`

因此 GCC 调用可能只被记录为普通 shell 执行，而不会成为验证记录。Trace 健康检查随后误将
本次情况概括为“没有验证命令”。

### 3.4 任务义务类型不足

当前 Trace 自动提取的任务义务主要包括：

- `verification_required`；
- `mcp_required`；
- `subagent_required`；
- `path_scope_exclusion`。

相关实现：

- `packages/opencode/src/observability/case-trace.ts::TaskObligationDraft`
- `packages/opencode/src/observability/case-trace.ts::taskObligationsFromInput`
- `packages/opencode/src/observability/case-trace.ts::evaluateTaskObligation`

目前没有结构化表达：

- 必须加载或遵循指定 Skill；
- 必须使用 Yocto/BitBake；
- GCC 只能作为局部诊断，不能代替权威构建；
- 替代验证必须说明等价性或降级理由。

## 4. 如何验证本次分析是否走错起点

对归因报告执行：

```bash
REPORT=/path/to/yocto-attribution.json

jq '{
  selected_starts: .analysis_question.selected_start_refs,
  start_refs,
  branches: [
    .defect_branches[]? | {
      start_ref,
      observed_defect_ref,
      analysis_outcome,
      termination_reason
    }
  ],
  conclusion,
  unresolved_gaps
}' "$REPORT"
```

如果起点和分支只有 `missing_semantic_final_test_result`、
`observed_defect_missing_verification_after_change`，则可以确认 Yocto 预期偏差没有被建模为
本次分析目标。单纯修改 `--question` 不能改变该事实。

## 5. 临时规避方案

在正式支持 `expectation_gap` 前，可使用离线 Review 显式注入一个质量偏差，再通过
`--start-ref` 限定归因起点，避免无关的自动 Trace 健康节点主导分析。

### 5.1 创建 Review 文件

先从 `trace.json` 或 `trace.html` 确认用户要求、模型上下文、Skill、决策和 GCC 工具调用的
真实 Trace 引用，然后创建 `yocto-review.json`：

```json
{
  "case_id": "<与 trace manifest 一致的 case-id>",
  "quality_review": {
    "status": "under_target",
    "total_score": 0,
    "target_score": 1,
    "attribution_objective": "分析为什么没有使用项目权威的 Yocto 构建流程，并确定下一次应修改的层次。",
    "quality_gaps": [
      {
        "dimension": "authoritative_build_workflow_adherence",
        "score": 0,
        "max_score": 1,
        "missing_evidence": [
          "没有执行 Yocto/BitBake 权威构建",
          "GCC 局部编译不能证明 Yocto 工程构建通过"
        ],
        "gap_context_refs": [
          "record:<用户要求节点>",
          "record:<模型请求上下文节点>",
          "record:<Skill 可用性或加载节点>",
          "record:<选择 GCC 的决策节点>",
          "record:<GCC 工具调用节点>"
        ]
      }
    ]
  }
}
```

尖括号占位符必须替换为该 Trace 中真实且可解析的引用。无效引用不会成为有效因果证据。

### 5.2 指定唯一分析起点

```bash
PYTHONPATH=/path/to/observable-opencode/tools/trace_attribution \
python -m trace_attribution \
  --engine recursive-agentic \
  --fusion-mode retrieval-global \
  --trace /path/to/case/trace.json \
  --review /path/to/yocto-review.json \
  --start-ref record:quality_gap_authoritative_build_workflow_adherence \
  --question "为什么执行路径偏离了项目的 Yocto 构建要求，下一次应改进哪个组件？" \
  --out /path/to/yocto-attribution.json
```

该方法只是为当前归因引擎提供一个明确起点，不代表归因模块已经具备完整的预期偏差分析
能力。输出仍需人工检查以下证据链是否完整：

```text
用户/项目约束
  -> 模型实际上下文
  -> Skill 可用性与加载结果
  -> Skill 内容中的 Yocto 约束
  -> Agent 工具选择决策
  -> GCC 调用及结果
  -> 最终验证或完成声明
```

## 6. 临时责任层判定规则

在正式模块实现前，可根据 Trace 事实人工判断最可能的改进层：

| Trace 事实 | 更可能的责任层 | 优先改进 |
| --- | --- | --- |
| 构建 Skill 不在可用列表 | Skill 安装或发现配置 | 修复 Skill 注册、路径和可见范围 |
| Skill 可用，但用户明确要求后仍未加载 | Agent/Harness 技能选择 | 增加 Skill 请求义务和调用检查 |
| Skill 已加载，但没有规定 Yocto | Skill 内容质量 | 写明权威命令、适用条件和完成标准 |
| Skill 明确要求 Yocto，且内容进入 LLM 上下文，但 Agent 无理由使用 GCC | Agent 指令遵循和决策 | 改进工具选择策略或增加执行门禁 |
| Skill 内容在上下文转换或压缩后丢失 | 上下文管理 | 将强约束标为不可淘汰义务 |
| Yocto 不可用，Agent 明确说明并使用 GCC 做局部诊断 | 合理降级 | 保留降级，但禁止宣称完成工程构建验证 |
| Yocto 只是未书面化的团队惯例 | 私域知识缺失 | 写入 Skill、项目说明或 Agent 指令文件 |
| Agent 用 GCC 成功后宣称完整验证通过 | 结果处理和完成判断 | 增加权威构建未执行时的完成声明门禁 |

需要区分：

- **偏差引入节点**：首次决定不走 Yocto、转而执行 GCC 的具体决策或动作节点；
- **长期改进责任层**：最适合提高下次成功率的 Skill、Agent、Harness、上下文或项目知识层。

二者不必是同一个位置。

## 7. 正式改进方向

建议在归因模块中增加独立的预期偏差分析模式，而不是将所有问题强制映射为功能缺陷：

```text
analysis_mode:
  defect_root_cause
  expectation_gap
  optimization_opportunity
```

`expectation_gap` 至少需要支持以下事实和结果字段：

- `expectation_contract`：预期内容及来源；
- `expectation_strength`：必须、默认、推荐或偏好；
- `expected_action`：预期的 Skill、工具和构建路径；
- `actual_action`：实际执行的 Skill、工具和构建路径；
- `strategy_equivalence`：替代路径是否覆盖原路径能力；
- `first_divergence_point`：预期路径与实际路径首次分叉节点；
- `selection_rationale`：选择、跳过或降级的理由；
- `responsibility_classification`：Skill、技能选择、上下文、Agent、Harness、环境或用户说明；
- `recommended_interventions`：针对下一次执行的可操作改进；
- `counterfactual_validation`：实施改进后是否更可能执行权威构建并满足验收条件。

Trace 侧需要增加或完善：

1. `skill_required`、`build_system_required`、`authoritative_verification_required` 任务义务；
2. Skill 的可用、请求、加载、内容进入上下文、约束被识别和后续采用状态；
3. GCC、Clang、Make、CMake、Ninja、BitBake、Kas 和 Yocto 环境初始化的命令分类；
4. 验证层级：语法检查、单文件编译、组件构建、工程构建、镜像构建；
5. 替代工具选择理由、失败后的降级理由及能力差异；
6. 最终完成声明所依赖的验证证据和权威性等级。

## 8. 后续验收标准

正式改造完成后，同类 Trace 的归因报告应能够：

1. 不被无关的 `missing_semantic_final_test_result` 默认起点劫持；
2. 明确展示 Yocto 要求是否进入每一层模型上下文；
3. 明确展示构建 Skill 是否可用、是否加载、内容是否要求 Yocto；
4. 找到从 Yocto 预期路径转向 GCC 的首次分叉节点；
5. 判断 GCC 是不等价替代、合理局部诊断还是有依据的降级；
6. 区分偏差引入节点与长期改进责任层；
7. 输出可执行的 Skill、Agent、Harness、上下文或项目知识改进建议；
8. 即使最终命令成功，也能识别流程遵循和验证覆盖方面的优化空间；
9. 在证据不足时明确列出缺失事实，而不是只输出泛化的 `evidence_insufficient`。

## 9. 范围声明

本文件是临时问题日志和正式改造输入，不表示上述 `expectation_gap` 模式、构建命令分类、
任务义务或改进建议字段已经在当前代码中实现。当前可用能力仍以仓库 README 和实际 CLI
schema 为准。
