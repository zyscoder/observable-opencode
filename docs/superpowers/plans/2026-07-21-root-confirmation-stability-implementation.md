# Root Confirmation Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将融合归因从“全局找到候选”提升为“对每个缺陷 seed 可独立确认根因、排除误报或明确最小证据缺口”。

**Architecture:** 在现有 `RecursiveAnalysisState` 内增加 per-seed ledger，不改变候选召回算法。Global Judge 输出结构化候选对比矩阵；缺证据时通过严格、有界的 expansion protocol 回查 Graph；Top 1 至 Top 3 候选分别进入独立确认，最后按 seed 汇总并校验关注点、竞争候选覆盖和因果路径。

**Tech Stack:** Python 3.12、dataclasses、现有 `TraceGraph`、`ClaudeCausalJudge`、checkpoint 与 `unittest`。

## Global Constraints

- 本计划依赖 `2026-07-21-trace-fact-closure-implementation.md` 全部验收通过。
- 归因过程保持离线、被动、只读，结果不可反馈给 Agent。
- 检索分数只能用于导航，不能进入独立根因确认请求。
- 只有可解析、revision 匹配、非纯时序的证据可以支持 confirmed root。
- 证据不足必须输出 `evidence_gap` 或 `inconclusive`，不得强制选择根因。
- 每个 seed 独立失败，不能覆盖其他 seed 已完成结果。
- 不引入新的 Python 依赖。
- 每个任务只提交该任务触及的文件，不混入工作区既有未提交改动。

---

### Task 1: Per-Seed Attribution 状态与报告模型

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py:45-60,1610-1985`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py:666-950,1680-1855`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`
- Create: `tools/trace_attribution/tests/test_seed_attribution.py`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/multi_seed_claims.json`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`

**Interfaces:**
- Produces: `SeedAttributionResult` dataclass。
- Produces: `RecursiveAttributionReport.seed_results: Tuple[SeedAttributionResult, ...]`。
- Produces: `RecursiveAnalysisState.seed_ledger: Dict[str, SeedAttributionBuilder]`。

- [ ] **Step 1: 写多 seed 不互相覆盖的失败测试**

```python
def test_report_preserves_independent_seed_outcomes(self):
    report = run_fixture("multi_seed_claims.json")
    by_ref = {item.start_ref: item for item in report.seed_results}

    self.assertEqual(by_ref["record:claim_tests"].outcome, "confirmed_root")
    self.assertEqual(by_ref["record:claim_change"].outcome, "no_defect")
    self.assertEqual(by_ref["record:claim_fragment"].outcome, "evidence_gap")
    self.assertEqual(report.analysis_outcome, "partial")
```

- [ ] **Step 2: 运行测试并确认 `seed_results` 不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_seed_attribution.py -v`

Expected: FAIL，`RecursiveAttributionReport` 没有 `seed_results`。

- [ ] **Step 3: 定义不可变 per-seed 结果**

```python
@dataclass(frozen=True)
class SeedAttributionResult:
    start_ref: str
    defect_fingerprint: str
    defect_state: DefectState
    outcome: str
    candidate_refs: Tuple[str, ...] = field(default_factory=tuple)
    selected_candidate_refs: Tuple[str, ...] = field(default_factory=tuple)
    confirmation_identities: Tuple[str, ...] = field(default_factory=tuple)
    confirmed_root_refs: Tuple[str, ...] = field(default_factory=tuple)
    decisive_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)
    blocking_reasons: Tuple[str, ...] = field(default_factory=tuple)
    global_judgment: JsonDict = field(default_factory=FrozenMapping)
    expansion_history: Tuple[JsonDict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in {
            "confirmed_root", "no_defect", "evidence_gap", "inconclusive"
        }:
            raise ValueError("unsupported per-seed attribution outcome")
```

`to_dict()` 与 `from_dict()` 必须完整往返；report schema 升为
`recursive-attribution-report/v3`，同时为 v2 输入提供只读迁移：按 `start_refs`
生成 `inconclusive` seed result，不伪造局部根因。

- [ ] **Step 4: 在 state 中维护 seed ledger 并定义顶层聚合规则**

每次 global pass、expansion、confirmation 和 unresolved branch 都以
`(start_ref, defect_fingerprint)` 更新对应 builder。顶层规则固定为：全部
`no_defect` -> `no_defect`；全部 `confirmed_root|no_defect` 且至少一个 root ->
`confirmed_root`；结果混合且没有未完成 seed -> `partial`；否则
`inconclusive`。

- [ ] **Step 5: 运行模型与 checkpoint 回归**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_causal_state.py tools/trace_attribution/tests/test_causal_checkpoint.py -v`

Expected: PASS。

- [ ] **Step 6: 提交 per-seed 模型**

```bash
git add tools/trace_attribution/trace_attribution/causal_state.py tools/trace_attribution/trace_attribution/recursive_analyzer.py tools/trace_attribution/trace_attribution/__init__.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): report outcomes per defect seed"
```

### Task 2: Global Candidate Comparison Matrix 与关注点校验

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/global_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py:2400-2520`
- Modify: `tools/trace_attribution/tests/test_global_judge.py`
- Modify: `tools/trace_attribution/tests/test_causal_judge.py`

**Interfaces:**
- Produces: `GlobalCandidateAssessment` 的 input/output defect status、causal path、counterfactual 与 competitor refs。
- Produces: `validate_active_focus_binding(request, judgment) -> None`。
- Consumes: Task 1 的当前 seed ref 和 defect fingerprint。

- [ ] **Step 1: 写关注点漂移和候选矩阵缺字段的失败测试**

```python
def test_rejects_judgment_that_answers_neighboring_claim(self):
    request = request_for_seed(
        seed_ref="record:claim_cstack",
        seed_text="_cstack returns the pre-computed matrix directly",
    )
    payload = valid_payload(request)
    payload["active_focus_binding"] = {
        "seed_ref": "record:claim_tests",
        "defect_fingerprint": request.active_defect.fingerprint,
        "evaluated_text": "All 11 tests pass",
    }
    with self.assertRaisesRegex(ValueError, "active focus"):
        validate_global_candidate_payload(payload, request=request)
```

另加测试，删除任一候选的 `input_defect_status`、`output_defect_status`、
`causal_path_refs`、`counterfactual` 或 `compared_candidate_refs` 后必须校验失败。

- [ ] **Step 2: 运行 Global Judge 测试并确认失败**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`

Expected: FAIL，旧 schema 不校验 active focus 和对比矩阵。

- [ ] **Step 3: 将 Global Judge schema 升为 v2**

`GlobalCandidateJudgeRequest` 增加 `seed_ref`、`active_defect`、
`active_focus_text` 和 `active_focus_text_hash`；hash 为规范化 seed 文本的 SHA-256。
`GlobalCandidateAssessment` 增加：

```python
input_defect_status: str       # present|absent|unknown
output_defect_status: str      # present|absent|unknown
causal_path_refs: Tuple[str, ...]
counterfactual: Mapping[str, Any]
compared_candidate_refs: Tuple[str, ...]
```

顶层 `GlobalCandidateJudgment` 增加：

```python
active_focus_binding: Mapping[str, Any]
```

binding 必须与 request 的 `seed_ref`、`defect_fingerprint` 和规范化 seed 文本 hash
完全一致。`candidate_roots` 只允许 assessment 满足
`input_defect_status != "present"` 且 `output_defect_status == "present"`。

- [ ] **Step 4: 在 prompt 中要求先比较、后选择**

Prompt 固定要求模型依次输出：输入缺陷状态、输出缺陷状态、真实下游路径、反事实、
与全部开放 authored candidates 的比较，最后才能选择 root。检索分数仅以
`retrieval_is_not_causal_verdict=true` 出现，不出现在选择规则中。

- [ ] **Step 5: 运行 Judge 与 analyzer 回归**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py -v`

Expected: PASS。

- [ ] **Step 6: 提交候选对比矩阵**

```bash
git add tools/trace_attribution/trace_attribution/global_judge.py tools/trace_attribution/trace_attribution/causal_judge.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_judge.py
git commit -m "feat(attribution): validate global causal comparison matrices"
```

### Task 3: 有界证据扩展协议

**Files:**
- Create: `tools/trace_attribution/trace_attribution/evidence_expansion.py`
- Create: `tools/trace_attribution/tests/test_evidence_expansion.py`
- Modify: `tools/trace_attribution/trace_attribution/global_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py:2024-2570`
- Modify: `tools/trace_attribution/trace_attribution/evidence_capsule.py`

**Interfaces:**
- Produces: `EvidenceExpansionRequest`、`EvidenceExpansionResult`。
- Produces: `expand_evidence(graph, request, limits) -> EvidenceExpansionResult`。
- Consumes: context kind `upstream|downstream|artifact|action_group|message_transform|full_node`。

- [ ] **Step 1: 写合法 anchor、去重和预算边界失败测试**

```python
def test_expands_only_requested_anchor_and_kind(self):
    request = EvidenceExpansionRequest(
        seed_ref="record:defect",
        defect_fingerprint="defect:v1",
        anchor_ref="record:decision",
        context_kind="upstream",
        reason="Need to determine whether the defect already existed in the decision input.",
        expected_judgment_change="root_candidate may become propagation_only",
    )
    result = expand_evidence(self.graph, request, ExpansionLimits(max_nodes=6, max_bytes=24000))
    self.assertTrue(result.items)
    self.assertTrue(all(item["direction"] == "upstream" for item in result.items))
    self.assertLessEqual(result.total_bytes, 24000)
```

无效 anchor、重复 request identity、`message_transform` 指向非 LLM 节点、artifact
超预算都必须返回结构化 rejection，不能静默扩展整张图。

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evidence_expansion.py -v`

Expected: FAIL。

- [ ] **Step 3: 实现 expansion domain types**

```python
@dataclass(frozen=True)
class ExpansionLimits:
    max_nodes: int = 8
    max_bytes: int = 32768

@dataclass(frozen=True)
class EvidenceExpansionRequest:
    seed_ref: str
    defect_fingerprint: str
    anchor_ref: str
    context_kind: str
    reason: str
    expected_judgment_change: str

    @property
    def identity(self) -> str:
        return stable_hash(self.to_dict())

def stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()
```

`expand_evidence()` 只能通过 `TraceGraph` 的 bounded adjacency、artifact hydration、
action group 或 message lineage 读取；输出每个 item 的 resolved ref、provenance、
truncation 和 bytes。纯时序、progress/navigation 捷径不得进入 causal expansion。

- [ ] **Step 4: 接入 Global Judge 重判循环**

每个 seed 默认最多 3 轮 expansion、每轮最多 8 节点和 32768 bytes。结果合并到 seed
Evidence Set 后重新调用 Global Judge。相同 request identity 不重复执行；预算耗尽时
seed outcome 为 `evidence_gap`，blocking reason 包含具体 request 和预算。

- [ ] **Step 5: 运行 expansion、capsule 和 analyzer 测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evidence_expansion.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_recursive_analyzer.py -v`

Expected: PASS。

- [ ] **Step 6: 提交有界扩展**

```bash
git add tools/trace_attribution/trace_attribution/evidence_expansion.py tools/trace_attribution/trace_attribution/global_judge.py tools/trace_attribution/trace_attribution/recursive_analyzer.py tools/trace_attribution/trace_attribution/evidence_capsule.py tools/trace_attribution/tests/test_evidence_expansion.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): add bounded causal evidence expansion"
```

### Task 4: Top-K 独立确认与完整竞争候选覆盖

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py:350-460,1990-2260`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py:3180-3820`
- Modify: `tools/trace_attribution/tests/test_causal_judge.py`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`

**Interfaces:**
- Consumes: Global Judge 选择的最多 3 个 authored root candidates。
- Produces: 每个候选独立 `RootConfirmation`，不携带 retrieval score。
- Produces: `repair_competitor_coverage(payload, request) -> JsonDict`，只补缺失比较项。

- [ ] **Step 1: 写检索分数隔离和 competitor 完整覆盖失败测试**

```python
def test_confirmation_request_excludes_retrieval_rank_and_covers_all_competitors(self):
    request = analyzer.build_confirmation_request_for_test(
        selected=("record:dec_4", "record:dec_2", "record:dec_17")
    )
    payload = stable_json(request.factual_dict())
    self.assertNotIn("retrieval_score", payload)
    self.assertNotIn("retrieval_rank", payload)
    self.assertEqual(
        {item["candidate_ref"] for item in request.competing_hypotheses},
        {"record:dec_2", "record:dec_17"},
    )
```

另加修复测试：模型漏掉一个 competitor 时，repair request 只包含缺失 competitor 的
固定身份，不允许改变 candidate status、evidence refs 或已有 comparison。

- [ ] **Step 2: 运行确认测试并记录旧版失败**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py -v`

Expected: 至少检索分数隔离或局部修复测试 FAIL。

- [ ] **Step 3: 扩展 confirmation factual context**

每个 request 必须包含：seed binding、candidate-local semantics、candidate 输入/输出
缺陷状态、真实 downstream path、支持/反对证据、counterfactual、开放 competitors、
artifact availability 和 revision status。不得包含全局 Judge 的 root verdict 或检索
排名。

- [ ] **Step 4: 实现严格但局部的 competitor 修复**

`repair_competitor_coverage()` 计算 expected identity 与实际 identity 差集，只请求
缺失项。修复响应合并前校验原 payload 中所有非 competitor 字段 hash 不变；如果
模型试图重写其他字段，拒绝修复并返回 unknown。

- [ ] **Step 5: 独立确认最多 3 个候选并协调结果**

每个候选单独调用确认。协调规则：一个 confirmed 且其他均 rejected -> primary root；
多个 confirmed 且 reciprocal comparison 均为 co_root -> co-roots；任何开放候选
unknown -> seed `evidence_gap`；没有 confirmed 且全部 unrelated/condition/amplifier ->
`inconclusive`，不得把贡献条件提升为 root。

- [ ] **Step 6: 运行确认回归**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_acceptance_review.py -v`

Expected: PASS，无 competitor schema retry loop。

- [ ] **Step 7: 提交独立确认改造**

```bash
git add tools/trace_attribution/trace_attribution/causal_judge.py tools/trace_attribution/trace_attribution/recursive_analyzer.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): independently confirm top causal candidates"
```

### Task 5: 阶段二回归与人工对照

**Files:**
- Modify: `tools/trace_attribution/tests/test_recursive_benchmarks.py`
- Create: `docs/superpowers/reports/2026-07-21-root-confirmation-stability-results.md`

**Interfaces:**
- Consumes: 阶段一重建后的 Axios、Astropy、TerminalBench Trace。
- Produces: 每个 seed 的人工结论、模块结论、候选覆盖、确认状态和证据缺口对照。

- [ ] **Step 1: 用离线 Fake Judge 回归协议和状态机**

覆盖 `confirmed_root`、`no_defect`、`evidence_gap`、`inconclusive`、多 seed partial、
provider failure、checkpoint resume 和 signal interruption。

- [ ] **Step 2: 运行完整 Python 测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`

Expected: 全部 PASS。

- [ ] **Step 3: 使用授权模型重跑三组归因**

每个 case 使用相同 objective、analysis perspective、模型、超时和预算；缓存目录隔离。
报告每个 seed 的 outcome、Top-K、confirmed root、missing evidence、逻辑/物理请求数和
token 使用。API key 只通过环境变量传入，不写入命令日志、Trace 或报告。

- [ ] **Step 4: 与冻结人工分析逐项比较**

Axios 必须保持 `no_defect`；Astropy 必须分别识别错误测试声明、有效修改声明和完整
`_cstack` claim，不再出现 seed 漂移；TerminalBench 必须把外部 oracle 纳入事实链，
并确认根因或准确指出仍缺失的候选局部事实。

- [ ] **Step 5: 检查阶段二验收门槛**

人工根因 Candidate Recall@24 为 100%；错误 confirmed root 为 0；per-seed 人工结论
一致率不低于 80%；因不明确 missing evidence 导致的 inconclusive 比当前下降至少
50%。未达标时报告具体 seed 和差异，不调整人工标签迎合模型。

- [ ] **Step 6: 提交阶段二结果**

```bash
git add tools/trace_attribution/tests/test_recursive_benchmarks.py docs/superpowers/reports/2026-07-21-root-confirmation-stability-results.md
git commit -m "test(attribution): evaluate per-seed root confirmation"
```

## Phase Completion Gate

只有阶段二报告满足错误 confirmed root 为 0，才能进入候选池成本收敛。若准确率
未达标，先定位是 Trace 事实缺口、候选漏召回、全局比较错误还是独立确认错误，
不得通过扩大候选池和模型上下文同时掩盖多个问题。
