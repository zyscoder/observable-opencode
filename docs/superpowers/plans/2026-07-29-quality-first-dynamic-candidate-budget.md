# Quality-First Dynamic Candidate Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以归因质量优先的动态预算确保真实 authored decision 不被密集近邻候选挤出，并将完整候选漏斗持久化到归因报告。

**Architecture:** 新增独立的候选预算事实模块，负责互斥分类、动态分档、确定性选择和审计身份；Global grounded closure 从 frontier 与相关 outcome 分支执行多锚点遍历，把发现与 offered slot 分离；现有 evidence capsule 只接收选择后的候选，并携带版本化漏斗指标。

**Tech Stack:** Python 3 标准库、冻结 dataclass、canonical JSON/SHA-256、现有 Causal IR TraceGraph、`unittest`。

## Global Constraints

- 归因保持被动、离线、只读，不得影响 Agent 执行或 Trace 生成。
- Global offered/保障预算按发现规模使用 `24/4`、`96/16`、
  `192/32`、`256/64` 四档。
- 只有活动修订、真实 causal edge 可用于 grounded decision 保障。
- provenance-only sibling 不得计入 grounded decision 保障。
- `SemanticPredecessorRetriever.retrieve()` 和 `CausalCandidate` schema 不变。
- root confirmation Top-3、non-root Top-3 和 Provider 请求预算本轮不变。
- completed checkpoint replay 不得增加 Provider 请求。
- 旧报告必须继续可读取。
- 不得持久化 API key 或 Provider credential。
- 保留共享脏分支中的既有改动，不得覆盖或回退。

---

### Task 1: 候选预算事实与确定性选择

**Files:**
- Create: `tools/trace_attribution/trace_attribution/candidate_budget.py`
- Create: `tools/trace_attribution/tests/test_candidate_budget.py`

**Interfaces:**
- Produces: `quality_first_candidate_budget(discovered_count)`.
- Produces: `CandidateBudgetSelection`.
- Produces: `classify_candidate(graph, candidate) -> str`.
- Produces: `select_global_candidates(graph, candidates, policy) -> CandidateBudgetSelection`.

- [ ] **Step 1: 写失败测试**

覆盖互斥分类、重复 ref 去重、四个 grounded decision 保障槽、空槽回流、稳定
选择身份以及 `discovered = offered + dropped` 守恒。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m unittest \
  tools.trace_attribution.tests.test_candidate_budget -v
```

Expected: FAIL，因为 `trace_attribution.candidate_budget` 尚不存在。

- [ ] **Step 3: 实现最小领域模型**

使用冻结 dataclass；`CandidateBudgetSelection.to_dict()` 必须输出
完整可重放漏斗使用 `candidate-budget-funnel/v2`，并用 `stable_json()` 计算
`selection_identity`。选择顺序不得依赖 set/dict 的非确定遍历。

- [ ] **Step 4: 运行测试确认 GREEN**

Run 同 Step 2，Expected: PASS。

### Task 2: Global 多锚点 grounded closure 与决策保障

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`

**Interfaces:**
- Consumes: `CandidateBudgetPolicy`, `select_global_candidates`.
- Changes: `_global_grounded_upstream_candidates(...)` 返回遍历发现的候选，不因
  前 24 个 offered slot 提前停止发现 grounded decision。
- Changes: `_global_candidate_pool(...)` 返回
  `(candidates, downstream_paths, candidate_funnel)`.

- [ ] **Step 1: 写 dense-first-hop 失败测试**

构造超过 24 个一跳候选和一条
`decision -> tool.call -> change -> observed_defect` 链。断言旧实现遗漏 decision。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m unittest \
  tools.trace_attribution.tests.test_recursive_analyzer.AgenticRecursiveAnalyzerTest.test_global_pool_reserves_grounded_two_hop_authored_decision_after_dense_first_hop -v
```

Expected: FAIL，Global pool 中没有目标 decision。

- [ ] **Step 3: 实现发现与 offered 分离**

Grounded closure 从 frontier 与所有相关 outcome branch 构造锚点，扫描硬上限
设为 4,096 条边，discovery 输出上限设为 256，最大遍历深度设为 8；只有
`is_confirmation_causal_edge(..., default_eligible=False)` 的边可继续传播。
多锚点按 edge round-robin 推进；扫描结束后先保障最多 64 个真实 grounded
decision，再按发现顺序补足 closure 输出，并交给 Task 1 的动态选择器。

- [ ] **Step 4: 增加确定性与兼容测试**

验证 route 去重、活动修订过滤、相同输入输出稳定，以及
`SemanticPredecessorRetriever.retrieve()` 行为未改变。

- [ ] **Step 5: 运行 focused tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m unittest \
  tools.trace_attribution.tests.test_candidate_budget \
  tools.trace_attribution.tests.test_causal_retrieval \
  tools.trace_attribution.tests.test_recursive_analyzer -v
```

Expected: PASS。

### Task 3: Candidate Funnel 持久化

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/tests/test_evidence_capsule.py`
- Modify: `tools/trace_attribution/tests/test_causal_state.py`

**Interfaces:**
- Changes: `candidate_compression_metrics(..., candidate_funnel=None)`.
- Produces: `candidate_compression["candidate_funnel"]`.

- [ ] **Step 1: 写失败测试**

断言 completed、bypassed 与 failed Global pass 的
`candidate_compression.candidate_funnel` 均保留 exact schema；报告
round-trip 后 `selection_identity` 不变。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m unittest \
  tools.trace_attribution.tests.test_evidence_capsule \
  tools.trace_attribution.tests.test_causal_state -v
```

Expected: FAIL，因为 metrics 尚不接受漏斗。

- [ ] **Step 3: 实现兼容持久化**

`candidate_funnel` 为可选字段。旧 metrics 保持原有键集合；早期 v1 漏斗只读
兼容；新生成漏斗使用 v2，并同步 exact schema 校验。只校验系统拥有的
`candidate_compression.candidate_funnel` 路径，业务 payload 同名字段不触发
审计校验。

- [ ] **Step 4: 运行 focused tests**

Run 同 Step 2，Expected: PASS。

### Task 4: 回放与本轮验收

**Files:**
- Modify: `docs/superpowers/reports/2026-07-29-quality-first-dynamic-candidate-budget-results.md`
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: 新候选漏斗与现有 benchmark evaluation 脚本。
- Produces: 当前根因召回、候选漏斗和回归结果。

- [ ] **Step 1: 运行完整测试**

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m unittest discover \
  -s tools/trace_attribution/tests
```

Expected: 至少 1,234 项测试通过，0 failures/errors。

- [ ] **Step 2: 运行 compileall**

```bash
PYTHONPATH=tools/trace_attribution:. python3 -m compileall -q \
  tools/trace_attribution/trace_attribution \
  tools/trace_attribution/scripts
```

Expected: exit 0。

- [ ] **Step 3: 回放 Sphinx、Seaborn、Pydantic**

优先使用已有本地 Trace，不调用 Agent。记录每个 case 的 discovered/offered、
reserved decision、Global gate、候选根因召回和最终根因结果。

- [ ] **Step 4: 写结果报告**

报告必须分别回答：

- Seaborn `dec_230` 是否进入候选池；
- Pydantic `dec_327` 是否进入候选池；
- 候选召回变化来自多锚点结构闭包、保障槽还是总预算扩大；
- 是否引入新的错误候选或 payload 膨胀；
- 下一轮如何进入 Judge 分页与分组淘汰，避免将 256 个候选一次发送。
