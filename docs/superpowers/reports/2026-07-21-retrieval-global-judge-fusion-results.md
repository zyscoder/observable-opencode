# Retrieval + Global Judge + Recursive Confirmation 融合迭代结果

## 1. 本轮目标

本轮将原有“候选检索后逐节点递归判断”改造成三段式融合流程：

1. 确定性检索先从 Causal IR 中召回有因果可能性的节点、结果反证和同轮 authored decision sibling。
2. 全局 LLM Judge 在有界 Evidence Capsule 上同时比较全部候选，输出 `candidate_roots`、`no_defect`、`needs_expansion` 或 `inconclusive`。
3. 只有全局入选的根因候选进入独立确认；证据不足时回退到原递归流程或保持 unknown，不强行发布根因。

该流程是离线、被动、不可反馈给 Agent 的分析逻辑，不改变 Agent 原始执行行为。

## 2. 已实现能力

- 新增 Candidate Evidence Capsule：候选语义、真实下游路径、入/出边、action group、证据引用及 artifact 可用性。
- 新增 Global Candidate Judge 严格 schema、缓存、修复重试和物理请求预算控制。
- CLI 新增 `--fusion-mode retrieval-global`，并将融合模式纳入 checkpoint 身份。
- 支持由成功验证反证派生缺陷，例如“缺少验证”误报。
- 支持评估节点之后追加的结果证据，避免离线诊断记录顺序遮蔽真实测试结果。
- 支持大工具结果中的嵌套命令、状态、结构化声明和输出预览检索。
- 支持同轮 authored decision sibling 召回，只使用共享的记录 provenance，不制造因果边。
- 使用真实、可归因、非纯时序边重建候选到缺陷的最短路径。
- 排除 temporal/progress/navigation 捷径进入独立确认。
- 将结果证据与根因资格分离：工具结果可作为条件或反证，但不能作为全局根因。
- 新增 `active_focus`，在多 claim 场景中固定当前 seed、defect fingerprint 和待审查文本。

## 3. 迭代中发现并修复的问题

### 3.1 Axios 验证结果召回失败

根因是 `TraceNode.compact()` 在大于 3200 字符时丢弃了 `args.command`、`metadata.preview` 和 `output.preview`。Trace 中存在 Mocha 成功结果，但离线检索层看不到。

修复后使用专用、有界的验证事实检索视图，不修改 Trace，也不影响 Agent。

### 3.2 派生诊断的时序边界错误

`missing_verification` 诊断位于图位置 441，真实测试结果位于 470/471。普通后向遍历的“只看当前位置以前”规则不适用于离线派生评估节点。

修复后，评估类 seed 可扫描完整 Trace 中的决定性反证，并记录其相对时序。

### 3.3 `no_defect` schema 过严

旧规则要求所有候选均为 absent，无法表达“诊断记录存在，但已被独立证据反驳”。

新规则允许 present 的 outcome observation 被 absent 的决定性 outcome/exculpatory evidence 反驳，同时禁止保留 present 的根因或贡献因子。

### 3.4 全局候选路径使用伪短路径

TerminalBench 中，图内已有 `decision -> tool -> result -> response -> observed defect` 的真实路径，但融合层曾拼接 `decision -> progress -> defect`，导致独立确认拒绝。

修复后使用 Causal IR 中可归因、非纯时序的最短下游路径，并排除 progress/navigation 路径捷径。

### 3.5 根因资格与贡献因子资格混用

直接修改共享 `root_candidate_eligible()` 会误删“失败工具结果”这一有效贡献条件。最终改为胶囊内独立的全局根因资格：结果节点仍参与比较，但不能被选为根因。

### 3.6 多 claim 审查发生语义漂移

共享 response segment 包含多条声明，LLM 曾对不同 seed 重复审查第一条声明。新增顶层 `active_focus` 后，前三个 Astropy claim 已按各自文本判断。

剩余漂移来自 Trace 将一条完整 `_cstack` 声明错误拆成两个 claim，其中第二段以逗号开头，不具备独立语义。

## 4. 真实 Benchmark 结果

### 4.1 Axios `axios__axios-5892`

人工判断：Trace 中实际执行了 `npx mocha`，退出状态为 0；“缺少验证”是派生诊断误报。

最终融合结果：`no_defect`。决定性证据为真实 bash tool result 及其 semantic fact；不再进入逐节点递归。

| 指标 | 旧递归版本 | 最终融合版本 |
|---|---:|---:|
| 结论 | inconclusive | no_defect |
| 访问节点 | 2 | 0 |
| 逻辑 Judge 调用 | 2 | 1 |
| 物理请求 | 4 | 2 |
| 候选节点 | 未形成全局集合 | 12 / 522 |
| 节点压缩率 | - | 97.70% |
| JSON 字节压缩率 | - | 95.26% |

结果文件：`/private/tmp/observable-opencode-fusion-eval-20260721-v1/axios-final/recursive.attribution.json`

### 4.2 Astropy `astropy__astropy-12907`

人工判断：

- `All 11 tests pass` 不成立。pytest 输出包含 1 个失败，只是 shell 退出码被掩盖为 0。
- “一字符修改 `= 1 -> = right`”有读、写和变更事实支撑。
- `_cstack` 行为解释被错误拆成两个原子 claim，第二段不是可独立评估的句子。

最终融合结果：

- 对“11 个测试通过”正确识别为 unsupported claim，并召回 response segment 根因候选。
- 对“一字符修改”正确输出 `no_defect`。
- 对 `_cstack` 前半句识别为 unsupported claim 候选，但独立确认认为 LLM call 只是生成载体，未确认根因。
- 对逗号开头的后半句仍发生语义漂移，暴露 claim atomization 缺陷。
- 最终保持 `inconclusive`，未发布错误根因。

| 指标 | 旧递归版本 | 最终融合版本 |
|---|---:|---:|
| 结论 | inconclusive | inconclusive，且给出逐 claim 证据判断 |
| 访问节点 | 8 | 0 |
| 逻辑 Judge 调用 | 10 | 6（4 次全局 + 2 次确认） |
| 物理请求 | 19 | 9 |
| 每个 seed 候选数 | - | 9 / 14 / 10 / 11 |
| 节点压缩率 | - | 97.37% - 98.31% |
| JSON 字节压缩率 | - | 96.82% - 98.18% |

结果文件：`/private/tmp/observable-opencode-fusion-eval-20260721-v1/astropy-final-focus/recursive.attribution.json`

### 4.3 TerminalBench `cancel-async-tasks`

人工判断：初始取消模型假设、实现写入、对失败测试的重新解释以及测试替代共同形成缺陷链；外部 process-level SIGINT oracle 不在 Trace 内。

最终融合结果：

- 全局 Judge 将 `decisionnode_dec_4_210528bd` 识别为首要根因候选。
- `dec_2` 被识别为初始设计贡献条件，`dec_17` 被识别为错误解释和测试替代贡献条件。
- 同轮 sibling 召回覆盖了人工标注的 `dec_18`，同时识别出它是重复工具执行投影而非独立根因。
- 工具结果不再被选为根因。
- 独立确认因外部 SIGINT oracle 和完整 artifact 未进入 Trace 包而返回 unknown，最终保持 `inconclusive`。

| 指标 | 旧递归版本 | 最终融合版本 |
|---|---:|---:|
| 结论 | inconclusive，无稳定候选 | inconclusive，有明确首要候选和证据缺口 |
| 访问节点 | 2 | 0 |
| 逻辑 Judge 调用 | 2 | 2（1 次全局 + 1 次确认） |
| 物理请求 | 4 | 3 |
| 候选节点 | - | 36 / 268 |
| 节点压缩率 | - | 86.57% |
| JSON 字节压缩率 | - | 67.44% |

结果文件：`/private/tmp/observable-opencode-fusion-eval-20260721-v1/terminalbench-v5/recursive.attribution.json`

## 5. 人工分析与模块分析对比

| Case | 正确候选是否进入集合 | 全局判断质量 | 独立确认质量 |
|---|---|---|---|
| Axios | 是，成功测试反证被召回 | 与人工一致 | 无需确认 |
| Astropy | 是，声明、响应、验证均进入集合 | 3/4 claim 聚焦正确；1 个受错误 atomization 影响 | 未强行确认错误根因 |
| TerminalBench | 是，人工关注的 `dec_2`、`dec_18` 及更直接的 `dec_4`、`dec_17` 均被覆盖 | 与人工因果链高度一致 | 正确停在外部 oracle / artifact 缺口 |

结论：当前融合方案已经证明“先大幅缩小日志，再让 LLM 全局比较，最后独立确认”是可行的。候选召回能力已明显强于旧版逐节点遍历，且能够在 Axios 上纠正误报；但最终根因确认仍受 Trace 事实闭包质量制约。

## 6. 当前剩余问题

1. Trace 包只保留 `partial/latest.json` 时，manifest 中的 artifact 路径存在但实体文件缺失，导致候选局部语义无法完整确认。
2. 外部 benchmark oracle 尚未作为带来源、时间和适用范围的事实节点进入 Causal IR。
3. 原子 claim 切分会在括号和逗号处生成不完整片段，破坏一一对应的缺陷状态。
4. 多根因确认的 `competitor_comparisons` 仍可能因覆盖不完整而触发 schema 失败。
5. TerminalBench 候选池达到 36 个、1.08 MB，虽比原 Trace 小 67.44%，但仍需要分层配额和去重。
6. 当前报告顶层只给一个聚合 outcome，多 seed 场景需要一等公民的 per-seed outcome，避免局部结果被总体状态掩盖。

## 7. 下一轮建议

### P0：Trace 事实闭包

- 修复 claim atomization：保留 `claim_group_id`、前后片段关系和原始响应 byte range；不生成以标点开头的独立 claim。
- Trace 打包时保存根因确认所需 artifact，或在 Causal IR 内嵌经过 hash/byte-range 标识的充分语义片段。
- 将外部 benchmark oracle 转换为 `external.evaluation_fact`，记录来源、适用 revision、观测方法和证据引用。

### P0：归因确认稳定性

- 为每个 seed 输出独立 outcome、候选、确认状态和阻塞原因。
- 修复多候选确认的 competitor coverage，保证修复重试能够补齐全部开放假设。
- 对 active-focus 漂移增加结果级校验，不只依赖提示词。

### P1：候选池成本控制

- 将全局池限制在 24-32 个，采用根因候选、反证、结果证据、同轮 sibling 的分层配额。
- 对共享 response/action group 只传一次公共证据，其余胶囊使用引用，减少重复字节。
- 持续统计 candidate recall@K、人工根因覆盖率、确认 precision、字节压缩率和物理请求数。

## 8. 验证

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests
Ran 487 tests in 2.599s
OK
```
