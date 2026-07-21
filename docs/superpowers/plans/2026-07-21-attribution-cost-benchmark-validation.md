# Attribution Cost and Benchmark Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不降低人工根因召回的前提下，将全局候选上下文收敛到 24 至 32 个，并用多类型开源 Benchmark 量化语义 Trace 与归因模块的准确性、压缩率和成本。

**Architecture:** 将候选选择改为分层配额，并把候选共享的 response、artifact 和 action group 提取到一次发送的 Evidence Catalog。建立可复现 Benchmark manifest、人工标签与自动评分器，对新旧归因进行盲化对照和消融实验，输出每个 case、每个 seed 和聚合指标。

**Tech Stack:** Python 3.12、现有 Trace attribution CLI、`unittest`、JSON/Markdown 报告、开源 Benchmark 已下载样本。

## Global Constraints

- 本计划依赖 Trace 事实闭包和根因确认稳定性两个阶段全部验收通过。
- Candidate Recall@24 未达到 100% 时不得进一步缩小候选池。
- 人工标签与归因输出分离保存，评分器不得把人工根因注入 Judge prompt。
- Benchmark 输入、模型版本、参数、代码 revision 和 Trace hash 必须可复现。
- API key 只从环境变量读取，不能写入仓库、Trace、缓存元数据或报告。
- 不下载或发送许可证不允许处理的私有数据。
- 每个任务只提交该任务触及的文件，不混入工作区既有未提交改动。

---

### Task 1: 分层候选配额

**Files:**
- Create: `tools/trace_attribution/trace_attribution/candidate_quota.py`
- Create: `tools/trace_attribution/tests/test_candidate_quota.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py:2178-2360`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`

**Interfaces:**
- Produces: `CandidateQuota` 和 `select_quota_candidates(candidates, quota, position_by_ref) -> Tuple[CausalCandidate, ...]`。
- Consumes: 现有候选的 event type、causal role hint、retrieval source、score 和 ref。

- [ ] **Step 1: 写各层保底和全局上限失败测试**

```python
def test_quota_preserves_roots_counterevidence_and_outcomes(self):
    selected = select_quota_candidates(
        candidates=self.candidates,
        quota=CandidateQuota(total=24, authored=10, propagation=4, outcome=4, exculpatory=3, sibling=3),
        position_by_ref=self.graph.position,
    )
    self.assertLessEqual(len(selected), 24)
    self.assertTrue({"record:manual_root", "record:failed_test", "record:passing_counterevidence"}.issubset(
        {item.ref for item in selected}
    ))
    self.assertEqual(len({item.ref for item in selected}), len(selected))
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_candidate_quota.py -v`

Expected: FAIL。

- [ ] **Step 3: 实现稳定分层和溢出回填**

```python
@dataclass(frozen=True)
class CandidateQuota:
    total: int = 24
    authored: int = 10
    propagation: int = 4
    outcome: int = 4
    exculpatory: int = 3
    sibling: int = 3
```

每层先按确定性 key `(-score, graph_position, ref)` 选择保底名额；未用名额回填到 authored
和高分剩余候选。相同 ref 只保留最高分候选，同时合并 retrieval sources 和 evidence
refs。任何层不得因满额丢弃唯一的 decisive counterevidence。

- [ ] **Step 4: 接入 CLI 可配置上限**

增加 `--global-candidate-limit`，默认 24，允许范围 8 至 64；checkpoint identity 必须
包含该值。分层比例按默认 24 固定，其他总量按比例取整并确保 authored 至少 8、
outcome 和 exculpatory 各至少 2。

- [ ] **Step 5: 运行 quota 与 analyzer 测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_candidate_quota.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_cli.py -v`

Expected: PASS。

- [ ] **Step 6: 提交候选配额**

```bash
git add tools/trace_attribution/trace_attribution/candidate_quota.py tools/trace_attribution/trace_attribution/recursive_analyzer.py tools/trace_attribution/trace_attribution/cli.py tools/trace_attribution/tests/test_candidate_quota.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_cli.py
git commit -m "perf(attribution): bound global candidates by causal role"
```

### Task 2: 共享 Evidence Catalog 去重

**Files:**
- Create: `tools/trace_attribution/trace_attribution/evidence_catalog.py`
- Create: `tools/trace_attribution/tests/test_evidence_catalog.py`
- Modify: `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- Modify: `tools/trace_attribution/trace_attribution/global_judge.py`
- Modify: `tools/trace_attribution/tests/test_evidence_capsule.py`
- Modify: `tools/trace_attribution/tests/test_global_judge.py`

**Interfaces:**
- Produces: `EvidenceCatalog`，键为稳定 `catalog_ref`。
- Produces: capsule 的 `shared_evidence_refs`，不重复内嵌共享 payload。
- Consumes: response segment、action group、artifact semantic slice、external evaluation fact。

- [ ] **Step 1: 写共享 payload 只序列化一次的失败测试**

```python
def test_shared_response_and_artifact_are_serialized_once(self):
    catalog, capsules = build_catalogued_capsules(self.graph, self.candidates)
    payload = stable_json({
        "evidence_catalog": catalog.to_dict(),
        "capsules": [item.to_dict() for item in capsules],
    })
    self.assertEqual(payload.count("shared final response body"), 1)
    self.assertEqual(payload.count("shared pytest artifact excerpt"), 1)
    self.assertTrue(all(item.shared_evidence_refs for item in capsules))
```

- [ ] **Step 2: 运行测试并确认重复 payload 仍存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evidence_catalog.py -v`

Expected: FAIL。

- [ ] **Step 3: 实现 Evidence Catalog**

Catalog entry 格式固定为：

```python
{
    "catalog_ref": "catalog:<sha256前16位>",
    "kind": "response_segment|action_group|artifact_slice|evaluation_fact",
    "source_refs": ["record:..."],
    "content": {...},
    "content_hash": "sha256:...",
    "availability": "complete|truncated|missing",
}
```

相同规范化 content hash 只保留一次。Capsule 内保留 candidate-local node、路径和边，
共享内容替换为 catalog refs。Global Judge 的 grounded refs 同时接受 candidate refs 和
catalog refs，但 root 只能选择 candidate ref。

- [ ] **Step 4: 增加 prompt 与 schema 校验**

Global Judge request 顶层增加 `evidence_catalog`。任何 catalog ref 必须解析；missing
entry 不能作为 decisive evidence；truncated entry 可以作为导航，但独立确认仍需
完整事实或明确反事实不依赖截断部分。

- [ ] **Step 5: 运行 catalog、capsule 和 Judge 测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evidence_catalog.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_global_judge.py -v`

Expected: PASS，fixture payload 字节数至少下降 30%。

- [ ] **Step 6: 提交 Evidence Catalog**

```bash
git add tools/trace_attribution/trace_attribution/evidence_catalog.py tools/trace_attribution/trace_attribution/evidence_capsule.py tools/trace_attribution/trace_attribution/global_judge.py tools/trace_attribution/tests/test_evidence_catalog.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_global_judge.py
git commit -m "perf(attribution): deduplicate shared causal evidence"
```

### Task 3: 可复现 Benchmark Manifest 与人工标签

**Files:**
- Create: `tools/trace_attribution/benchmarks/manifest.json`
- Create: `tools/trace_attribution/benchmarks/labels.schema.json`
- Create: `tools/trace_attribution/benchmarks/labels/axios__axios-5892.json`
- Create: `tools/trace_attribution/benchmarks/labels/astropy__astropy-12907.json`
- Create: `tools/trace_attribution/benchmarks/labels/terminalbench-cancel-async-tasks.json`
- Create: `tools/trace_attribution/trace_attribution/benchmark_manifest.py`
- Create: `tools/trace_attribution/tests/test_benchmark_manifest.py`

**Interfaces:**
- Produces: `load_benchmark_manifest(path) -> BenchmarkManifest`。
- Produces: 每个 case 的 immutable input hash、trace path、evaluation path、人工 seed 标签和允许根因集合。

- [ ] **Step 1: 写 manifest 完整性失败测试**

```python
def test_manifest_binds_inputs_and_manual_labels_by_hash(self):
    manifest = load_benchmark_manifest(MANIFEST)
    self.assertGreaterEqual(len(manifest.cases), 3)
    for case in manifest.cases:
        self.assertRegex(case.trace_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(case.manual_labels)
        self.assertEqual(len({item.seed_ref for item in case.manual_labels}), len(case.manual_labels))
```

- [ ] **Step 2: 运行测试并确认 manifest 不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_benchmark_manifest.py -v`

Expected: FAIL。

- [ ] **Step 3: 定义 manifest 与 label schema**

每个 case 固定包含：`case_id`、`benchmark`、`source_url`、`license`、`revision`、
`trace_path`、`trace_sha256`、`evaluation_paths`、`label_path`、`allowed_remote_fields`。
`trace_path` 和 evaluation paths 均相对 `OPENCODE_BENCHMARK_ROOT`，manifest 不依赖
`/private/tmp` 或开发机绝对路径；runner 在执行前校验文件 hash。
每个人工 seed 标签包含：`seed_ref`、`defect_status`、`accepted_root_refs`、
`accepted_contributing_refs`、`decisive_evidence_refs`、`minimum_missing_evidence` 和
`rationale`。

- [ ] **Step 4: 固化三组当前人工分析**

Axios 标签为缺少验证误报、预期 `no_defect`；Astropy 分别标记测试声明错误、修改
声明有证据、完整 `_cstack` claim；TerminalBench 标记 `dec_4` 为主要候选，`dec_2`
和 `dec_17` 为贡献条件，并绑定外部 SIGINT oracle。标签文件不得包含模型输出。

- [ ] **Step 5: 运行 schema 和 hash 校验**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_benchmark_manifest.py -v`

Expected: PASS。

- [ ] **Step 6: 提交 Benchmark manifest**

```bash
git add tools/trace_attribution/benchmarks tools/trace_attribution/trace_attribution/benchmark_manifest.py tools/trace_attribution/tests/test_benchmark_manifest.py
git commit -m "test(attribution): freeze benchmark cases and manual labels"
```

### Task 4: 人工分析与模块分析评分器

**Files:**
- Create: `tools/trace_attribution/trace_attribution/benchmark_scoring.py`
- Create: `tools/trace_attribution/tests/test_benchmark_scoring.py`
- Create: `tools/trace_attribution/scripts/score_benchmark_matrix.py`

**Interfaces:**
- Produces: `score_case(report, labels) -> CaseAttributionScore`。
- Produces: candidate recall、root precision、per-seed agreement、false confirmation、compression 和 request/token 成本。

- [ ] **Step 1: 写精确评分失败测试**

```python
def test_scores_candidate_recall_and_false_confirmation_separately(self):
    score = score_case(
        report=report_with(candidates=["root", "noise"], confirmed=["noise"]),
        labels=labels_with(accepted_roots=["root"]),
    )
    self.assertEqual(score.candidate_recall, 1.0)
    self.assertEqual(score.root_precision, 0.0)
    self.assertEqual(score.false_confirmed_roots, 1)
```

- [ ] **Step 2: 运行测试并确认评分模块不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_benchmark_scoring.py -v`

Expected: FAIL。

- [ ] **Step 3: 实现 seed 级和 case 级指标**

评分器输出：`candidate_recall_at_k`、`root_precision`、`root_recall`、
`per_seed_outcome_agreement`、`false_confirmed_roots`、`evidence_gap_precision`、
`candidate_node_reduction_ratio`、`candidate_byte_reduction_ratio`、
`logical_judge_calls`、`physical_requests`、`input_tokens`、`output_tokens`。聚合报告同时
提供 macro average 和总计，不能用 case 数量较多的 Benchmark 淹没其他类型。

- [ ] **Step 4: 实现命令行评分脚本**

```text
python3 tools/trace_attribution/scripts/score_benchmark_matrix.py \
  --manifest tools/trace_attribution/benchmarks/manifest.json \
  --results /private/tmp/observable-opencode-benchmark-results \
  --out /private/tmp/observable-opencode-benchmark-score.json
```

脚本只读取 attribution JSON 和人工标签，不调用 LLM。

- [ ] **Step 5: 运行评分器测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_benchmark_scoring.py -v`

Expected: PASS。

- [ ] **Step 6: 提交评分器**

```bash
git add tools/trace_attribution/trace_attribution/benchmark_scoring.py tools/trace_attribution/tests/test_benchmark_scoring.py tools/trace_attribution/scripts/score_benchmark_matrix.py
git commit -m "feat(attribution): score causal analysis against manual labels"
```

### Task 5: 扩展 Benchmark 类型与消融实验

**Files:**
- Modify: `tools/trace_attribution/benchmarks/manifest.json`
- Create: `tools/trace_attribution/scripts/run_benchmark_matrix.py`
- Create: `tools/trace_attribution/tests/test_benchmark_matrix.py`
- Create: `docs/superpowers/reports/2026-07-21-attribution-cost-benchmark-results.md`

**Interfaces:**
- Consumes: 至少六个 case，覆盖代码修改、语义理解、长上下文、多工具或多 Agent。
- Produces: full、no-artifact、no-external-fact、no-global-judge、no-confirmation 五组结果。

- [ ] **Step 1: 增加至少三类开源 Benchmark case**

优先选择仓库已允许处理且已有本地样本的 SWE-bench/FeatureBench 代码修改 case、
需求或文档语义冲突 case、长上下文或多步骤工具 case。每类至少一个 case；每个 case
都必须先由人工独立标注 seed、根因候选、贡献条件和决定性证据，再加入 manifest。

- [ ] **Step 2: 写矩阵命令构造失败测试**

断言 runner 为每个 case 生成五种独立输出目录，所有运行共享模型和预算，但使用
不同 cache/checkpoint；resume 不得复用不同 ablation 的缓存。

- [ ] **Step 3: 实现可恢复矩阵 runner**

Runner 读取 manifest，对每个 case 和 ablation 调用 attribution CLI。每次运行保存：
命令参数（隐藏 key）、git revision、trace hash、evaluation hash、模型、base URL host、
预算、开始/结束时间、退出状态和产物路径。已有完整且 hash 匹配的结果直接跳过。

- [ ] **Step 4: 先运行离线 Fake Judge 回归**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`

Expected: 全部 PASS。

- [ ] **Step 5: 在授权后运行真实模型矩阵**

真实运行前确认允许发送的开源字段，API key 通过环境变量传入。每个请求 timeout
保持 3600 秒；provider error 记录并可恢复，不转换为归因结论。

- [ ] **Step 6: 评分并完成消融分析**

报告必须回答：

1. 正确根因是否始终进入 Top 24；
2. 共享 catalog 减少多少字节和 token；
3. Artifact、external fact、Global Judge、confirmation 各自对准确率的贡献；
4. 人工与模块不一致来自 Trace、召回、比较还是确认；
5. 哪些冗余信息可继续删除，哪些缺失事实必须新增。

- [ ] **Step 7: 检查最终验收指标**

Candidate Recall@24 必须为 100%；错误 confirmed root 必须为 0；per-seed 人工结论
一致率不低于 80%；Trace 节点压缩率不低于 90%；Evidence Catalog 相对重复 capsule
字节数下降至少 30%；因证据缺失导致的 inconclusive 比融合基线下降至少 50%。

- [ ] **Step 8: 提交 Benchmark 结果**

```bash
git add tools/trace_attribution/benchmarks/manifest.json tools/trace_attribution/scripts/run_benchmark_matrix.py tools/trace_attribution/tests/test_benchmark_matrix.py docs/superpowers/reports/2026-07-21-attribution-cost-benchmark-results.md
git commit -m "test(attribution): validate causal attribution across benchmarks"
```

## Final Completion Gate

三个阶段完成后，最终结论必须基于：冻结人工标签、可复现 Trace hash、真实归因输出、
自动评分和消融实验。若某项指标未达标，报告保留失败结果，并将下一轮问题明确归类
为 Trace 事实缺口、候选召回缺陷、全局比较缺陷、确认缺陷或成本问题。
