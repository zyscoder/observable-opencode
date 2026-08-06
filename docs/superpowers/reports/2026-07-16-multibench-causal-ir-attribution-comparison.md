# 多基准 Causal IR 与离线归因对照评测

## 1. 评测目的

本轮不直接修改语义 Trace 或归因模块，而是先扩大真实开源 Benchmark 覆盖，比较：

1. 官方评测结果；
2. 人工后向语义污点分析；
3. 离线归因模块的自动分析；
4. Trace 中缺失、错误或过度冗余的语义信息。

所有 Case 均通过 `opencode serve -> HTTP session -> HTTP message` 执行。归因只在执行结束后离线进行，不写回 Trace，也不向 Agent 反馈。

## 2. 环境与样本

- Docker 运行时：Colima `observable-benchmark`，`linux/amd64`，8 CPU、16 GiB、120 GiB；
- Agent：当前分支本地构建的 observable-opencode；
- 模型：`deepseek-v4-pro`；
- 归因模型接口：DeepSeek Anthropic-compatible endpoint；
- 单个 FeatureBench Case HTTP 预算：1800 秒；
- 归因参数：`max_depth=24`、`max_nodes=128`、单次 Judge 超时 3600 秒；
- 本轮归因 Judge 错误/超时：0。

| Case | Benchmark | 领域 | 官方/稳定结果 |
| --- | --- | --- | --- |
| `gin-gonic__gin-2121` | SWE-bench Multilingual | Go Web | resolved，1/1 |
| `axios__axios-5892` | SWE-bench Multilingual | JavaScript HTTP | resolved，1/1 |
| `astropy__astropy-12907` | SWE-bench Verified | Python 科学计算 | resolved，1/1 |
| `cancel-async-tasks` | TerminalBench | Python asyncio/SIGINT | 官方时序受 amd64 仿真干扰；稳定复现实验确认清理失败 |
| `pydantic...test_deprecated_fields...lv1` | FeatureBench lite | Python 数据模型 | unresolved，空补丁，0% |
| `sphinx...test_domain_c...lv1` | FeatureBench lite | Python 解析器/符号表 | unresolved，空补丁，0% |

该样本用于验证 Trace 和归因能力，不应当外推为模型在完整 Benchmark 上的总体分数。

## 3. 总体结论

### 3.1 任务结果

- 3 个 SWE-bench Case 全部通过官方评测；
- 1 个 TerminalBench Case 在稳定复现实验中确认存在真实语义缺陷；
- 2 个 FeatureBench 复杂多接口 Case 均在 30 分钟内零写入、空补丁退出；
- 本轮 6 个样本中严格成功 3 个，但样本是为正负对照选择，不能视为模型通过率。

### 3.2 Trace 能力

- 对 3 个真实失败/低质量 Case，人工均能从 Trace 定位到具体决策层根因，人工可归因率为 3/3；
- Trace 已能记录模型推理、工具调用、代码变更、测试、上下文转换和 LLM 调用时长，作为人工取证档案基本有效；
- 但终态缺陷到决策、动作、变更、验证之间缺少选择性因果边，因此尚不是自动归因可直接使用的因果图。

### 3.3 离线归因能力

- 3 个真实失败 Case 的精确根因命中率为 0/3；
- 3 个成功负样本没有产生错误的 Agent 根因，假根因数为 0；
- 所有 6 个 Case 的总体结果均为 `inconclusive`，说明当前模块偏保守，能避免武断根因，但尚不能稳定提供可行动结论；
- 新增两个 FeatureBench Case 未触发深度/节点上限，Judge 也无错误，失败原因不是预算不足，而是图连通性、节点语义角色和遍历策略不足。

## 4. 逐例人工与自动归因对照

### 4.1 TerminalBench：真实实现缺陷

人工根因：

- `decisionnode_dec_2_ba104d23` 认为“首次 `gather` 被取消后，再逐个 `task.cancel()` 并重新 `gather`”仍可保证异步 `finally` 完成；
- 实际上第一次取消已传播到子任务，第二次取消可能中断 `finally` 中的 `await`；
- `decisionnode_dec_18_fe917370` 设计的自测使用直接 `main.cancel()`，且清理动作没有 `await`，无法覆盖进程级 SIGINT 中断异步清理的机制。

自动结果：

- 能识别最终“清理一定执行”的陈述存在缺陷；
- 随后沿宽泛的 `response -> context.transform -> context.transform` 链回溯；
- 没有访问人工根因决策、写入和变更节点，最终 `inconclusive`。

结论：Trace 中存在根因语义，但最终声明缺少到具体实现决策和验证设计的选择性支持边。

### 4.2 FeatureBench Pydantic：重复搜索导致零交付

人工根因：

- `decisionnode_dec_97_1a78831c` 已明确识别 `computed_field` 等接口缺失，却选择寻找被禁止/不存在的上游实现；
- `decisionnode_dec_102_22855594` 在已经知道 editable install 指向 `/testbed` 后，再次搜索已安装副本；
- `decisionnode_dec_109_03a79d8f` 到约 20.8 分钟才改为依据接口说明直接设计实现，之后仍继续读/搜，直至超时；
- 16 个已完成 LLM 调用累计 1,163,969 ms，最慢一次 482,819 ms，是重要外部放大因素，但不能解释已经观察到的重复决策错误。

自动结果：

- 访问 9 个节点，主要是终态 `prepare_llm_turn`、消息转换和上下文集合；
- 没有访问 `dec_97`、`dec_102` 或 `dec_109`；
- 将多个普通消息转换误标为“传播空补丁缺陷”，随后又被独立确认器驳回，最终 `inconclusive`。

结论：缺少“任务义务、进展、停滞、剩余预算、重复搜索 episode”语义；终态也没有连接到导致零进展的决策 episode。

### 4.3 FeatureBench Sphinx：已理解但持续探索

人工根因：

- `decisionnode_dec_85_1b4ffa67` 在约 4.7 分钟时声明已完整理解五个待实现方法；
- 此后选择继续阅读辅助方法，而不是对已理解接口进行写入；
- `decisionnode_dec_92_c8c86bb6`、`decisionnode_dec_99_d18fc6bf` 反复确认缺失方法并扩大探索范围，全程零写入；
- 已完成 LLM 调用累计 940,817 ms，最终调用被截止信号取消。

自动结果：

- 仅访问离线 observed defect 与 `case_failed_83980b48` 两个节点；
- `case.failed` 的 16 条上游引用几乎全部是生命周期收尾节点，未链接最近有效决策或未完成义务；
- 没有访问 `dec_85`，最终 `inconclusive`。

结论：`case.failed` 当前描述“如何收尾”，没有描述“为什么未交付”。

### 4.4 三个成功负样本

- Gin：补丁通过官方评测，但 Trace 因外部 Sidecar/SIGINT 标记失败；模块未误报 Agent 根因，但也不能明确输出 `root_outside_trace`；
- Axios：补丁和 `npx mocha` 验证均成功，但命令分类器未识别 `npx mocha`，Trace 错报缺少验证；模块保持 unknown/inconclusive，未能主动驳回错误健康告警；
- Astropy：4 个最终声明中 3 个判断为 `no_defect`；另 1 个判断同时写出“声明准确描述缺陷与修复”以及 `defect_status=present`，存在 Judge 输出语义自相矛盾。

## 5. Trace 中有效、缺失和冗余的信息

### 5.1 已有价值

- 推理块保留了“为什么选择某个工具”的文本，可人工识别错误假设；
- 工具调用、结果、代码变更和验证记录基本可审计；
- LLM 调用级耗时和 token 汇总可用于区分 Agent 策略与 Provider 延迟；
- Sphinx 在 SIGINT 时把未闭合记录标为 cancelled/finalized，保留了中断现场；
- 原始流式 delta 仅保留在 `events.jsonl`，没有继续膨胀为 Causal IR 语义节点，这一分层是正确的。

### 5.2 关键缺失

1. **任务义务**：FeatureBench Pydantic 有 3 个 Interface Description，Sphinx 有 4 个描述、5 个方法，但两份 Trace 的 `task_obligations` 均为 0。
2. **进展与停滞**：没有 `first_understood_at`、`first_write_at`、未完成义务数、重复搜索 episode、无进展时长和剩余预算。
3. **终态因果链接**：`case.failed`、外部评分、最终响应没有直接连接到最近的有效决策、动作、代码变更、验证或未完成义务。
4. **Evidence consumption**：Subagent 和工具结果虽被记录，但缺少“哪条后续决策实际使用了哪条证据”。
5. **LLM 延迟分解**：只有总时长，缺少请求开始、首 token、首工具调用、最大 token 间隔、Provider retry/rate-limit/cancel source。
6. **外部执行信封**：Trace 内没有统一记录 HTTP deadline、并发 Case、主机/容器负载、官方评测结果和超时来源。
7. **Claim 完整性**：TerminalBench 最关键的错误清理保证未被抽取为 `response.claim`。
8. **验证语义**：`npx mocha` 和任意 Python 验证脚本未被稳定识别，导致成功 Case 产生错误健康告警。

### 5.3 冗余或降权项

- Pydantic 的 `context.pack + context.transform` 为 181/572 个节点，Sphinx 为 172/542，约占 32%；这些节点对取证有用，但当前过度占据自动回溯路径；
- `case.failed` 指向大量生命周期节点，表达收尾完整性，却几乎不表达任务质量因果；
- Sphinx 的通用 `function-return/return-value` 冲突组把不同工具返回值视为同一语义冲突，属于低价值噪声；
- 生命周期、消息封装、Provider 转换节点应保留在档案层，但默认不应成为任务质量污点的传播节点；
- 宽泛 Context Set 应作为候选证据容器，只有被具体决策消费的成员才进入归因链。

## 6. 下一轮建议

建议按以下顺序迭代，并继续保持“被动记录、事后归因、不可反馈给 Agent”。

### P0：建立终态到决策 episode 的因果主链

1. 生成离线 `progress.episode`：按“理解/搜索/实现/验证/收尾”聚合决策与动作；
2. 为每个 episode 记录输入义务、选择的策略、实际动作、进展增量、耗时和退出原因；
3. `case.failed` 与官方 evaluation outcome 指向最后一个有语义的 episode、未完成义务和在途 LLM 调用，不再只指向 lifecycle；
4. 响应声明指向具体变更、验证及产生该声明的决策 episode。

### P0：补齐多接口义务与预算事实

1. 从 Prompt 的 Interface Description、路径和符号被动抽取 `task.obligation`；
2. 事后计算 obligation 的 observed/read/implemented/verified 状态；
3. 记录 case deadline、elapsed、remaining、first-write、no-progress interval；
4. 将“重复搜索同一来源”“已理解但未行动”作为离线派生事实，不改变 Agent 行为。

### P1：归因模块遍历与判定收敛

1. 遍历优先级改为 `evaluation -> progress episode -> decision/action -> change -> verification`；
2. lifecycle、provider transform、broad context 默认降权，只有存在显式 consumption 才传播任务质量污点；
3. 增加 active-defect 一致性校验：若 Judge 同时称声明“准确/真实”且角色为 `defect_evidence`，不得保留 `defect_status=present`；
4. 对 `root_outside_trace`、`external_timeout_contributor`、`trace_health_false_positive` 建立一等结论，避免一律落入 inconclusive。

### P1：延迟、验证与退出收尾

1. 为 `llm.call` 增加 TTFT、token 间隔、retry/status、cancel source；
2. 验证分类改用规范化可执行命令与结果语义，覆盖 `npx mocha`、`python script.py` 等形式；
3. 修复 `serve` 收到 SIGINT/SIGTERM 后生成 canonical `trace.json` 并及时退出；本轮两个 FeatureBench 容器均最终升级到 SIGKILL，Pydantic 只留下状态仍为 running 的 partial Trace；
4. 保留 raw events 作为取证层，但不让 delta 和行政节点进入默认任务质量回溯路径。

## 7. 判定

当前 Causal IR 已经达到“人工可观测、人工可归因”的阶段，但尚未达到“自动稳定归因”。核心矛盾不是缺少原始事件，而是缺少从任务义务和进展 episode 到终态结果的选择性因果主链。下一轮应优先改图的语义组织与归因遍历，而不是继续增加更多通用事件或扩大 `max_depth/max_nodes`。
