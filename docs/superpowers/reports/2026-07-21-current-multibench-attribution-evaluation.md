# 当前多 Benchmark 语义 Trace 与归因效果评估

## 评估范围

本轮使用当前分支的离线归因模块，对 6 个来自不同开源 Benchmark 的既有
Trace 进行重新分析，并将模块结果与人工后向语义分析对照：

| Case | Benchmark | 评估重点 |
| --- | --- | --- |
| `gin-gonic__gin-2121` | SWE-bench Multilingual | 中断生命周期兼容性 |
| `axios__axios-5892` | SWE-bench Multilingual | 变更后的验证证据 |
| `django__django-13023` | SWE-bench Verified | 多轮失败、修复与回归验证 |
| `astropy__astropy-12907` | SWE-bench Verified | 最终声明与证据闭包 |
| `pydantic...test_deprecated_fields...lv1` | FeatureBench | 中断 Trace 的保守归因 |
| `cancel-async-tasks` | TerminalBench | 实现假设和测试设计缺陷 |

这些 Trace 大多由较早版本的采集器生成。因此，本轮主要验证当前归因模块对
历史数据的兼容性和可用性；历史 Trace 中的缺口不能直接证明当前采集器仍存在
相同问题。

## 量化结果

| Case | 结果 | 访问节点 | 物理/逻辑 LLM 调用 | 确认根因 | 人工对照结论 |
| --- | --- | ---: | ---: | ---: | --- |
| Gin | `inconclusive` | 0 | 0/0 | 0 | 正确保守识别缺少 `process.signal`，但无法分析任务质量 |
| Pydantic | `inconclusive` | 0 | 0/0 | 0 | 同 Gin，未虚构 Agent 根因 |
| Axios | `inconclusive` | 2 | 4/2 | 0 | Trace 已有 Mocha 验证，起始“缺少验证”事实为假阳性 |
| Django | `inconclusive` | 2 | 4/2 | 0 | Trace 有失败后修复及两组成功测试，起始事实为假阳性 |
| Astropy | `inconclusive` | 8 | 19/10 | 0 | 4 条最终声明均有证据，模块未得到 `no_defect` |
| TerminalBench | `inconclusive` | 2 | 4/2 | 0 | 找到两个高价值决策面，但协议校验阻断根因确认 |

汇总：

- 决定性结果为 0/6，确认根因数为 0。
- 共访问 14 个语义节点，产生 31 次物理请求和 16 次逻辑调用。
- 修复/重试额外消耗 15 次请求，占物理请求的 48.4%。
- 共有 9 条未解决分支、7 个 `unknown` 判断。
- 6/6 没有虚构确认根因，安全性较好，但有效性不足。
- 与上一轮相同 5 个 Case 相比，物理调用由 32 降至 27，逻辑调用由 18
  降至 14；成本分别下降 15.6% 和 22.2%，但决定性结果没有改善。

## 人工分析与模块结果

### Axios 和 Django

两份 Trace 都记录了变更后的验证行为。Axios 执行了目标 Mocha 用例；Django
先记录失败，再修改测试，随后目标测试和表单 DecimalField 测试均成功。人工
分析应把起始节点判为 `trace_health_false_positive` 或直接返回 `no_defect`。

当前模块优先路由到最新 progress episode，绕过了更有决定性的验证结果，并在
第二个决策节点上反复产生“当前缺陷不存在，但关系仍为递归传播”的自相矛盾
响应。结论是：原始语义基本够用，主要缺陷在起始事实校验和 Judge 协议。

### Astropy

Trace 的 pytest 结果表明 11 个测试通过，四条最终声明均可被变更和验证证据
支持。人工结论应为 `no_defect`。

模块把每个 `response.claim` 直接转换为标签为 `observed_defect` 的缺陷状态，
其中 `actual` 就是声明文本，例如 `All 11 tests pass.`。这混淆了“待检查的
声明”和“已经存在的声明质量缺陷”，导致正常声明被沿成功变更反向传播。
此外，两条声明仍因 Judge 状态与关系不一致而进入 `unknown`。

### TerminalBench

人工分析可从外部 SIGINT 复现向后定位到：

1. 实现决策假设再次取消、再次 gather 不会打断已经开始的异步清理；
2. 替代测试只模拟直接取消父协程，没有复现进程级 SIGINT 的时序。

当前检索已把 `dec_17`（测试设计）和 `dec_2`（实现假设）选入前两名，候选发现
质量明显提升。但 `dec_17` 的判断被输入 provenance 的词法校验规则拒绝，
`dec_2` 被判无缺陷，最终没有根因。这说明数据流导航已有价值，节点缺陷判断和
根因确认仍不可靠。

### Gin 和 Pydantic

旧 Trace 没有独立 `process.signal` 节点。模块不调用 LLM、不把 `case.failed`
当作自身原因，并明确返回 `process_signal_node_missing`。这是正确的保守兼容
行为，但只能形成 Trace 健康诊断，不能形成任务缺陷归因。

## 语义 Trace 质量

当前 Trace 对人工分析已经有实用价值：决策、工具调用、工具结果、变更、验证和
最终声明等主干信息基本可见。主要问题有三类：

1. **派生事实一致性不足。** Axios/Django 的“缺少验证”与原始工具结果矛盾，
   归因前缺少原始事实与派生事实的确定性校验。
2. **历史 Bundle 不可移植。** Gin、Axios、Astropy、Pydantic、Django 的
   manifest 虽列出 artifact，但本地文件存在率为 0%；TerminalBench 为
   51/129（39.5%）。预览足以辅助人工阅读，却不足以独立复核所有大文本。
3. **动作身份不稳定。** 某些 reasoning、tool call、tool execute、tool result
   节点缺少贯穿全生命周期的稳定动作标识，人工可凭时序识别，程序映射容易偏移。

判断：Trace 主干覆盖中等偏好，语义一致性一般，Bundle 可移植性差。它目前适合
人工取证，但尚不能被归因模块无条件当作可信事实源。

## 归因模块质量

当前版本的优点是保守、可审计、不会为了给出答案而强行确认根因；进度导航和
候选排序也已能在 TerminalBench 中找到人工关注的决策面。主要短板是：

- 起始节点没有按事件类型构造“候选缺陷”，正常 response claim 被当成既成缺陷；
- 缺陷存在性、前驱关系、缺陷引入和根因确认仍挤在一个 Judge 协议内；
- 依赖 reason 文本关键词的 provenance 校验过于僵硬；
- progress 优先策略没有根据缺陷类型调整，会绕过更强的验证证据；
- 大量修复调用仍无法把矛盾响应变成有效判断。

判断：候选发现进入可用阶段，缺陷判定与根因确认仍处于实验阶段。当前版本适合
生成待审查假设，不适合直接输出自动根因结论。

## 下一轮改进

### P0：修正起始语义和事实一致性

1. 为不同起始事件构造不同候选缺陷。`response.claim` 应检查
   `unsupported_response_claim`，而不是直接标成 `observed_defect`。
2. 在 LLM 前增加确定性的 Trace 健康校验：当工具结果明确记录测试通过时，
   拒绝或降级“没有验证”的派生节点。
3. 根据缺陷类型选择入口：验证缺失类先走 verification/tool outcome，外部行为
   缺陷再走 progress/decision，进程中断只走 signal/lifecycle。

验收标准：Astropy 四条声明全部判为无缺陷；Axios、Django 得到 `no_defect`
或明确的 `trace_health_false_positive`。

### P0：拆分 Judge 协议

将一次复杂判断拆成三个独立阶段：

1. 当前节点是否存在候选缺陷；
2. 哪个前驱对缺陷产生了传播、转换或实质贡献；
3. 当前节点是否为缺陷引入点，并进行独立根因确认。

用结构化 `candidate_local_defect` 和证据引用替代对 reason 文本的正则校验。

验收标准：最终协议校验失败为 0，修复/重试请求低于物理请求的 20%。

### P0：保证 Artifact 闭包

Trace finalization 时校验所有 `artifact_refs` 和 manifest 路径，原子复制缺失
文件并写出完整度摘要。存在缺失 artifact 时，不应把 Bundle 标记为完整。

验收标准：新生成 Bundle 的引用文件存在率至少 99.9%，归因报告明确区分
“证据不存在”和“证据未加载”。

### P1：统一动作身份并扩充回归集

为 reasoning、tool call、tool execute、tool result、change、verification 增加稳定
`action_group_id` 和等价节点映射。使用当前采集器重新执行成功、失败、中断、
工具失败、错误测试设计五类 Case，避免只在历史 Trace 上优化兼容逻辑。

验收标准：TerminalBench 的人工根因 Top-3 召回为 2/2，至少确认一个可操作根因；
成功 Case 保持零假根因并能稳定输出 `no_defect`。

## 评测产物

本轮归因结果位于：

```text
/private/tmp/observable-opencode-benchmark-eval-20260721-current-multibench-v2/
  gin/recursive.attribution.json
  pydantic/recursive.attribution.json
  axios/recursive.attribution.json
  django/recursive.attribution.json
  astropy/recursive.attribution.json
  terminalbench/recursive.attribution.json
```
