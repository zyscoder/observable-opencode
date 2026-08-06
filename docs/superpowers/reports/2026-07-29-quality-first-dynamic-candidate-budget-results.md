# Quality-First 动态候选预算迭代结果

## 1. 本轮目标

本轮只验证离线归因的候选事实层：

1. 从失败结果向后重建多条语义数据流；
2. 召回具有真实因果路径的 authored decision；
3. 在候选密集时按质量优先策略扩大预算；
4. 完整记录 discovered、offered、dropped 与保障原因；
5. 不调用 Agent，不改变 Harness 行为，不向 Provider 发送请求。

本轮不宣称最终根因确认已经完成。256 个候选不能作为一个超大请求直接发送给
LLM，Judge 分页和分组淘汰是下一轮任务。

## 2. 实现变化

### 2.1 多锚点因果闭包

旧实现主要从当前 frontier 回溯。真实大 Trace 中，frontier 常被最新
`progress_episode` 占据，而实现 change、验证结果和完成声明位于其他 outcome
分支。仅从单一 frontier 回溯会遗漏这些分支。

新实现从以下锚点共同执行 bounded upstream closure：

- 当前 frontier；
- 与当前 seed downstream path 相连的已知候选；
- 可通过真实 causal edge 回到 seed 的相关 outcome 分支。

扫描最多 4,096 条 predecessor edge、深度最多 8，只接受
`is_confirmation_causal_edge(..., default_eligible=False)` 的活动修订边。
多锚点按 edge round-robin 推进；扫描后先保留最多 64 个 deep grounded
decisions，再按发现顺序把 closure discovery 输出补到 256。

### 2.2 质量优先动态预算

| discovered | offered limit | grounded decision reserve |
| ---: | ---: | ---: |
| `0..24` | 24 | 4 |
| `25..96` | 96 | 16 |
| `97..192` | 192 | 32 |
| `193+` | 256 | 64 |

grounded decision 先按确定顺序占用保障槽，剩余候选按原始发现顺序回填。所有
发现候选仍保留在漏斗审计中，裁剪不会抹去“曾发现但未 offered”的事实。

## 3. 真实 Benchmark 回放

回放使用既有原始 Trace 和人工评审事实：

- Sphinx sanitized recursive case；
- FeatureBench Lite Seaborn regression case；
- FeatureBench Lite Pydantic deprecated-fields case。

Seaborn 与 Pydantic 通过现有 `inject_quality_gap_records()` 注入已经冻结的
人工 observed-defect 事实，保持与历史 v2 归因运行的起点一致。回放只构建
`TraceGraph`、`RecursiveAnalysisState` 与 Global candidate pool，Provider
请求数为 0。

### 3.1 Sphinx

两个 frontier 的漏斗均为：

| discovered | offered | dropped | policy |
| ---: | ---: | ---: | --- |
| 5 | 5 | 0 | `24/4` |

`record:decision` 在两个分支中都被识别为 grounded decision，并提升到 offered
rank 0。路径分别为：

```text
decision -> change -> verification -> observed
decision -> change -> verification -> timeout -> observed
```

小图不会因为动态策略无条件膨胀。

### 3.2 Seaborn

| Seed pass | discovered | offered | dropped | grounded | reserved |
| --- | ---: | ---: | ---: | ---: | ---: |
| 实现失败分支 | 298 | 256 | 42 | 64+ | 64 |
| 验证/完成分支 | 291 | 256 | 35 | 64+ | 64 |

四个人工根因的候选结果：

| 人工根因 | 对应分支结果 | 关键路径 |
| --- | --- | --- |
| `dec_230` | discovered、reserved、offered | `decision -> edit tool_call -> observed defect` |
| `dec_235` | discovered、reserved、offered | `decision -> edit tool_call -> observed defect` |
| `dec_426` | discovered、offered；至少一分支 reserved | `decision -> verification fact -> observed defect` |
| `dec_439` | 在验证/完成分支 discovered、offered | completion decision 到 observed defect 的活动路径 |

因此 Seaborn 人工根因的候选覆盖由历史的 `3/4` 提升到跨 seed 合并后的
`4/4`。`dec_439` 不要求在实现失败 seed 中出现；它属于验证/完成分支。

### 3.3 Pydantic

| Seed pass | discovered | offered | dropped | grounded | reserved |
| --- | ---: | ---: | ---: | ---: | ---: |
| 实现失败分支 | 287 | 256 | 31 | 31 | 31 |
| 验证/中断分支 | 259 | 256 | 3 | 17 | 17 |

人工根因 `record:decisionnode_dec_327_a0b88b89` 在两个 seed 中均
discovered、reserved、offered。对应活动路径包括：

```text
dec_327 -> implementation action -> chg_7 -> observed defect
dec_327 -> implementation action -> chg_7 -> verification -> observed defect
```

Pydantic 人工根因候选覆盖由历史的 `0/1` 提升到 `1/1`。

## 4. 效果判断

三个 case 共 6 个人工根因，本轮候选池跨对应 seed 的合并覆盖为 `6/6`。这是
候选召回结果，不是最终根因 precision/recall。

改善来自两个缺一不可的变化：

1. 多锚点闭包让实现与验证 outcome 分支都能被遍历；
2. 动态保障槽把已发现的深层 decision 提升到 offered 池。

只扩大 total limit 并不能保证正确 decision 不被近邻噪声再次挤出；只保留
4 个 decision 槽也不足以覆盖大 Trace 中数十个 grounded decision。

## 5. 风险与边界

1. 大图仍会发现 259 至 298 个候选，最多裁到 256，语义负载仍然很大。
2. grounded 只表示存在合格因果路径，不等于该节点一定有缺陷或一定是根因。
3. 64 个保障 decision 中仍含较多相邻计划、修复和完成决策，必须由 LLM
   比较缺陷、传播关系和独立引入性。
4. Seaborn 某些长路径经过 context/final response 节点。路径是图上合格边，
   但还需要 Judge 对机制相关性作语义判断。
5. 本轮没有执行 Provider 判断，不能用候选召回提升替代最终归因质量结论。

## 6. 下一轮

下一轮进入质量优先 Judge 分页：

1. 每页保留完整 evidence capsule，建议 24 至 32 个候选；
2. 每页独立判断缺陷与上游传播，保留多假设而非单胜者；
3. 将各页高置信候选和 unresolved 候选汇入 finalist pool；
4. 用跨页比较和独立根因确认收敛最终根因；
5. 持久化 page identity、候选进入/淘汰理由和 Provider request lineage；
6. 再用 Sphinx、Seaborn、Pydantic 做人工分析与 LLM 归因对照。

该阶段的目标是把当前“候选召回充分”推进到“LLM 能在充分候选中稳定确认
根因”，同时避免一次性超大 prompt。

## 7. 回归验证

```text
focused candidate/funnel/integration: 130 tests passed
full trace_attribution suite:          1267 tests passed
compileall:                            passed
git diff --check:                      passed
```

最终回放的新漏斗 schema 均为 `candidate-budget-funnel/v2`。Sphinx 两个 pass
的 discovered/offered/dropped 为 `5/5/0`；Seaborn 为 `291/256/35` 与
`298/256/42`；Pydantic 为 `259/256/3` 与 `287/256/31`。
