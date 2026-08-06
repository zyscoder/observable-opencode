# Quality-first Global Judge 分页与跨页收敛实施计划

## Task 1：确定性分页模型

**文件**

- 新建 `tools/trace_attribution/trace_attribution/candidate_paging.py`
- 新建 `tools/trace_attribution/tests/test_candidate_paging.py`

**TDD**

1. 先写 0、1、4、5、8、9、24、25、48、256 个候选的分页测试。
2. 验证每个候选恰好出现一次、页面最多 4 个。
3. 验证稳定 identity 和输入顺序敏感性。
4. 实现 `CandidatePage`、`CandidatePagePlan` 和严格反序列化校验。

## Task 2：页内 survivor 事实投影

**文件**

- 修改 `candidate_paging.py`
- 修改 `test_candidate_paging.py`

**TDD**

1. 覆盖 selected、supported、unresolved、factor、excluded 五类投影。
2. 验证 unresolved 不受 finalist 软上限影响。
3. 验证投影只读取 canonical assessment 字段，不读取 reason 进行启发式判断。
4. 实现 `CandidatePageOutcome` 与 round summary。

## Task 2A：候选与证据上下文分流

**文件**

- 修改 `candidate_budget.py`
- 修改 `evidence_capsule.py`
- 修改 `global_judge.py`
- 修改 `recursive_analyzer.py`

**TDD**

1. 无 active-seed 因果路径的节点保留为 `evidence_context`，不进入 offered。
2. assessment 与 context capsule 互斥、无重复，并共同参与 grounding。
3. comparison contract 只要求 assessment capsule 逐项判断。
4. funnel v3 和 validation envelope v10 进入持久化身份。
5. 历史 funnel v1/v2 保持签名只读兼容。
6. verification、同轮兄弟决策等反证节点不得因分流而丢失。
7. 评审声明引用在当前 Trace revision 全部失效时，绑定到最终结果节点并
   持久化 `source_binding` 审计；不得引入人工根因标签。

## Task 2B：发现预算与评审预算解耦

**文件**

- 修改 `candidate_budget.py`
- 修改 `recursive_analyzer.py`
- 修改 `evidence_capsule.py`
- 修改相应测试

**TDD**

1. grounded factual closure 至少可返回 512 个节点，不得被 256 个 LLM
   assessment 上限提前截断。
2. `grounded_decision_refs` 是调用方提供的质量顺序，预算器和持久化校验器
   都必须保留该顺序，不得转成集合或重排为发现顺序。
3. 当前评审层仍最多提供 256 个候选，优先保留有完整事实路径、靠近结果
   边界的 authored decisions；扩充发现预算不得自动增加 LLM 请求数。
4. 使用超过 300 个浅层事实和 64 个 authored decisions 的合成 Trace，
   验证晚期 grounded decision 仍进入 offered。
5. 在 Pydantic 与 Seaborn 实际 Trace 上检查人工根因候选召回，但人工标签
   不得进入候选发现、排序或 Judge prompt。

## Task 3：分页 Judge 执行器

**文件**

- 修改 `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- 修改 `tools/trace_attribution/tests/test_recursive_analyzer.py`

**TDD**

1. 25 个候选触发 7 个请求，每个请求不超过 4 个 capsule。
2. 第一页失败时第二页仍执行。
3. 预算不足时未执行页显式记录，结果不发布全局 winner。
4. 将单次 seed 调用改造成页面循环，保留现有 evidence expansion。
5. 页内没有根候选但 active defect 未被推翻时返回
   `no_root_candidates`；不得误用 `no_defect`。
6. Global Candidate Judgment 升级到 v10，旧 v9 checkpoint 要求重新判断。

## Task 4：页面级 checkpoint

**文件**

- 修改 `recursive_analyzer.py`
- 修改 `tools/trace_attribution/trace_attribution/checkpoint.py`
- 修改 `tools/trace_attribution/tests/test_causal_checkpoint.py`

**TDD**

1. completed 页面恢复时 provider 调用为 0。
2. started 页面恢复时按 interrupted 记账且不重复请求。
3. 页面 identity、候选集合或预算不一致时拒绝恢复。
4. 单页 terminal action 不能冒充其他 round/page。
5. 新增独立 `global_judge_page_*` lifecycle，旧 `global_judge_*` 保持只读兼容。
6. 页面只预留 `min(remaining, 4)`，中断不能吞掉其他页面预算。
7. 页面策略参数进入 checkpoint config fingerprint。

## Task 5：跨页 finalist 收敛

**文件**

- 修改 `candidate_paging.py`
- 修改 `recursive_analyzer.py`
- 修改相应测试

**TDD**

1. 多页 supported roots 进入下一比较轮。
2. finalist 超过 48 时通过新一轮 Judge 比较，不做分数截断。
3. 连续两轮 survivor 集合不变时 stalled，并保留全部候选。
4. 最终仅在不超过 4 个 supported finalists 且无阻塞页面时执行。
5. 最终最多 3 个候选进入现有独立根因确认。

## Task 6：审计输出与兼容投影

**文件**

- 修改 `recursive_analyzer.py`
- 修改 `tools/trace_attribution/trace_attribution/causal_state.py`
- 修改 `test_causal_state.py`

**TDD**

1. journal 包含 page plan、page、round summary、convergence。
2. 旧 `global_candidate_pass` 入口仍存在，并绑定最终 judgment。
3. 所有新增事件声明 `none_offline_analysis_only`。
4. report round-trip 与 checkpoint replay 保持严格一致。

## Task 7：验证与 benchmark

1. 运行分页单测和 Global Judge 相关测试。
2. 运行 `tools/trace_attribution/tests` 全量测试。
3. 重放 Sphinx、Seaborn、Pydantic benchmark trace。
4. 对照人工根因，报告：
   - candidate coverage
   - page completion
   - finalist coverage
   - unresolved preservation
   - provider request count
   - checkpoint replay request count
5. 邀请独立子 Agent 审查事实保真、预算记账与 Agent 行为隔离。
