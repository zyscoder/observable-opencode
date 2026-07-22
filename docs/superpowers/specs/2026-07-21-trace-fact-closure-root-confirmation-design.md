# Trace Fact Closure + Root Confirmation Design

## 1. 背景

当前离线归因链路已经能够从完整 Causal IR 中高召回地筛选候选，并通过
Global Judge 比较候选的因果角色。在 Axios、Astropy 和 TerminalBench 三类真实
Benchmark 上，人工关注的关键节点均进入了候选集合，节点压缩率达到
86.57% 至 98.31%。

现阶段的主要瓶颈已经从“找不到候选”转为“候选缺少可闭合的事实，无法稳定确认
或排除根因”。具体表现为：

- 一条完整响应声明可能被拆成不具备独立语义的 claim；
- Trace manifest 记录了 artifact 路径，但独立复制 Trace 后文件不可读取；
- 外部 Benchmark oracle 没有进入 Causal IR；
- 多 seed 结果被顶层聚合状态覆盖；
- 多候选确认偶尔因 competitor 覆盖不完整而失败；
- 归因模型可能从当前 seed 漂移到共享响应中的其他 claim。

本轮采用“Trace 事实闭包 + 分层根因确认”方案。先补齐归因所需的被动事实，再
增强离线确认协议。插装与归因均不得改变 Agent、Harness 或 Benchmark 的原始
行为。

## 2. 目标与非目标

### 2.1 目标

1. 每个缺陷 seed 都具有语义完整、来源明确、可解析的证据闭包。
2. 归因模块能够区分根因引入节点、传播节点、放大因素、结果证据和反证。
3. 证据不足时输出具体缺口，不因日志邻近关系强行确认根因。
4. 保持候选高召回的同时，降低全局比较与独立确认的上下文成本。
5. 通过真实开源 Benchmark 回归，量化人工分析与模块分析的一致性。

### 2.2 非目标

- 不把归因结论、读取目的或后验判断反馈给 Agent。
- 不为了归因改变工具调用、模型消息、任务编排或测试执行逻辑。
- 不在本轮训练专用归因模型。
- 不把纯时间相邻关系升级为因果边。

## 3. 方案选择

### 3.1 备选方案 A：只优化归因提示词

改动小，但无法补回缺失 artifact、外部 oracle 和错误 claim 边界，效果上限低。

### 3.2 备选方案 B：事实闭包与分层确认协同改造

先完善 Trace 事实，再依次执行候选召回、全局比较、按需扩展和独立确认。该方案
兼顾可解释性、准确性和成本，是本轮采用的方案。

### 3.3 备选方案 C：将压缩后的大 Trace 直接交给更强模型

实现简单，但容易产生关注点漂移，成本随 Trace 规模增长，且难以验证判断依据。

## 4. 总体架构

```text
Agent / Harness 原始执行
        |
        v
被动语义插装 -----> Causal IR + Artifact Bundle + External Evaluation Facts
                              |
                              v
                         缺陷 Seed 列表
                              |
                              v
                    确定性数据流与候选召回
                              |
                              v
                      Candidate Evidence Set
                              |
                              v
                     Global Comparative Judge
                        |               |
                  证据充分           证据缺失
                        |               |
                        |        Bounded Evidence Expansion
                        |               |
                        +-------<-------+
                              |
                              v
                    Independent Root Confirmation
                              |
                              v
                       Per-Seed Attribution
```

## 5. Trace 事实闭包

### 5.1 Claim Group 与安全原子化

每个 `response.claim` 新增或稳定输出以下事实：

- `claim_group_id`：同一完整语义声明的稳定分组标识；
- `claim_index` 和 `claim_count`：组内顺序；
- `raw_text` 和 `canonical_text`；
- `source_byte_range`：在原始响应中的 UTF-8 字节范围；
- `previous_claim_ref` 和 `next_claim_ref`；
- `atomization_status`：`atomic`、`group_required` 或 `invalid_fragment`；
- `atomization_reason`：采用当前边界的确定性规则。

切分器必须保护括号、代码片段、文件路径、版本号和缩写。不得把以逗号、分号、
右括号等开始的片段作为独立 claim。无法可靠切分时，以完整 claim group 作为
评估 seed，不生成伪原子事实。

原子化内部采用 source-preserving Markdown block lexer + 单遍 Claim State Machine，
不允许先删除 Markdown 再计算 span。block lexer 使用仓库 catalog 中固定版本的
`marked@17.0.1`，只读取同步 lexer token，不渲染 HTML、不安装全局 extension。
每个 `token.raw` 必须按顺序映射回 `ClaimSourceView` 的原始字符范围；解析异常、
raw 无法无歧义映射或未知 block token 必须 fail-closed 为 `HARD_BREAK`，不得让 Trace
插装中断 Agent 执行。

`marked` 会把 CRLF 规范化为 LF，因此 raw 映射必须使用单调、newline-aware cursor：
lexer raw 中的 `\n` 可以消费原文当前位置的 `\n` 或 `\r\n`，其他字符必须逐字符
完全相等。映射只返回原文 char range，不得先全局规范化响应，否则 UTF-8 byte range
将失去可逆性。

`ClaimSourceView.scanEnd` 除不得截断 UTF-16 代理对外，也不得落在 `\r|\n` 之间；
命中 CRLF 中间时回退到 `\r` 之前。嵌套 list 的 child raw 可能被 marked 去除公共
缩进，必须使用父 item 原始范围内的逐物理行、indentation-aware 单调映射，禁止对
完整去缩进 raw 使用无范围文本搜索。

适配层只向 Claim State Machine 输出 `TEXT`、`PROTECTED_TEXT`、`SOFT_BREAK` 和带
来源类型的 `HARD_BREAK`。paragraph 进入 inline tokenizer；ATX/Setext heading、
thematic break、fence/indented code、HTML block、link definition、空段落和 blockquote
整体成为 hard barrier；list item 在 item 起止处产生 barrier，并递归处理 item 内
paragraph；table 在完整 table block 边界内继续生成带原始范围的 table fact。普通
物理换行仅在括号未闭合时延续 claim。`PROTECTED_TEXT` 的内容进入 claim 原文，但其
内部括号和标点不参与外层状态迁移。所有 byte range 始终指向原始响应。

GFM table 的语义 cell 必须来自 `marked` table token 的结构化 `header`/`rows`，禁止
再次通过 `split("|")` 解析。每个 data row 的 `raw_text` 和 byte range 仍来自按顺序
映射的原始物理行；escaped pipe 和多字节 cell 不得改变列数。若未转义 pipe 位于
inline code 并导致 marked 结构与原始 code span 无法一一证明，整行必须 fail-closed，
禁止通过文本搜索或补全猜测 cell 语义。

原始响应必须先封装为 `ClaimSourceView`，统一持有完整原文、Unicode 安全的扫描
终点和字符位置到 UTF-8 字节位置的映射。8,000 UTF-16 code unit 的扫描上限不得
截断代理对；扫描窗口之外不生成 claim，但窗口内的 `source_byte_range` 仍以完整
原始响应为坐标。反引号 code span 若在当前物理行内找不到同长度闭合 delimiter，
则从 opening delimiter 到行末整体记为 `PROTECTED_TEXT`，不得让 malformed Markdown
中的括号或标点污染外层状态机。

任意 `unknown` response 的规范化必须是 total function：JSON 序列化和显式字符串
转换都失败时使用固定 ASCII fallback `[unserializable response]`，不得因 hostile
`toJSON`、`toString` 或 `Symbol.toPrimitive` 让被动 Trace 抛错。

CaseTrace 为原子化暂存的 response 原文只能存活到 `finish()`。final、non-final、
cancelled、异常和重复 finish 路径都必须通过同一个生命周期清理点释放暂存原文；
清理不得改变既有摘要、artifact 外置、Causal IR 或 Agent 执行行为。

### 5.2 Artifact Bundle

归因所需 artifact 必须随 Trace 包一起可解析。每项 artifact 记录：

- `artifact_id`、相对路径和内容类型；
- `content_hash`、总字节数和保存策略；
- 已内嵌或单独保存的语义片段及 `byte_range`；
- 生成节点、读取节点、使用节点和 revision；
- `availability`：`embedded`、`bundled`、`external`、`missing` 或 `truncated`；
- 截断原因和缺失字节范围。

Trace 快照发布采用 manifest 与 artifact bundle 的一致性校验。复制一个 case 的
Trace 目录后，不依赖原工作区绝对路径即可读取所有已声明为 bundled 的证据。

### 5.3 External Evaluation Fact

Benchmark、grader 或人工评审结论统一投影为 `external.evaluation_fact`：

```json
{
  "event_type": "external.evaluation_fact",
  "source": "terminalbench",
  "scope": "process_sigint_behavior",
  "subject_revision": "git:abc123",
  "assertion": "SIGINT cancels active tasks and exits within the grader deadline",
  "observation": "the process remained alive after the grader deadline",
  "status": "failed",
  "observed_at": "2026-07-21T12:00:00Z",
  "evidence_refs": ["artifact:terminalbench-grader-output"],
  "provenance": {"method": "benchmark_grader", "version": "1.0"}
}
```

该节点只能作为 outcome evidence、反证或归因起点，不能仅因评测失败而自动成为
根因。必须校验 `subject_revision` 与 Trace 对应执行版本一致。

### 5.4 被动观测约束

Trace 新字段只从执行过程中已经存在的数据复制或确定性派生。以下内容不得进入
Agent 可见上下文：

- 后验 `read_purpose`、`used_by_decision` 等归因判断；
- Global Judge 或独立确认输出；
- Benchmark oracle 的根因解释；
- 候选排序、置信度或缺陷标签。

## 6. 分层根因确认

### 6.1 Per-Seed Attribution

每个 seed 独立维护：

- 活跃缺陷指纹和原始文本；
- 候选集合与压缩指标；
- 全局比较结果；
- 证据扩展历史；
- 独立确认结果；
- 最终状态：`confirmed_root`、`no_defect`、`evidence_gap` 或
  `inconclusive`；
- 阻塞原因和最小缺失证据。

顶层 outcome 仅汇总，不得覆盖任一 seed 的局部结论。

### 6.2 候选对比矩阵

Global Judge 必须对每个候选回答：

1. 当前缺陷在候选输入中是否已经存在；
2. 候选输出是否新增、传播、放大或纠正该缺陷；
3. 候选与 seed 是否存在可归因的数据流路径；
4. 移除或修正候选是否会阻断缺陷传播；
5. 哪些证据支持或反对该判断；
6. 与其他开放候选相比，解释力差异是什么。

候选角色限定为 `root_candidate`、`contributing_condition`、
`amplifying_factor`、`outcome_evidence`、`exculpatory_evidence`、
`unrelated` 或 `unknown`。

### 6.3 有界证据扩展

Judge 只能请求以下一种具体上下文：

- `upstream`；
- `downstream`；
- `artifact`；
- `action_group`；
- `message_transform`；
- `full_node`。

请求必须指定 anchor、缺失事实和该事实可能改变的判断。扩展结果进入同一 seed 的
Evidence Set，随后重新执行全局比较。达到预算仍无法闭合时输出 `evidence_gap`，
不强行确认。

### 6.4 独立根因确认

Global Judge 选出的 Top 1 至 Top 3 根因候选分别确认。确认请求不得携带检索分数，
但必须包含：

- 当前 seed 和候选的完整局部语义；
- 缺陷进入候选前后的状态；
- 候选到 seed 的真实数据流路径；
- 支持、反对和反事实证据；
- 所有仍开放的竞争候选。

只有同时满足以下条件才发布 confirmed root：

1. 候选自身引入当前缺陷；
2. 缺陷不能由候选输入中的既有缺陷充分解释；
3. 候选不是纯执行载体、结果节点或时间邻近节点；
4. 候选到 seed 存在可审计因果路径；
5. 竞争候选已逐项排除或降级为贡献条件；
6. 决定性证据均可解析并与执行 revision 一致。

### 6.5 防漂移与协议修复

- Judge 输出必须绑定 `active_focus.seed_ref` 和 defect fingerprint；
- 判断理由必须引用当前 claim 文本或 evaluation assertion；
- 禁止用共享 response 中其他 claim 替代当前 seed；
- `competitor_comparisons` 必须覆盖全部开放候选且每个候选只出现一次；
- 修复重试只补齐缺失字段，不得改变已经通过校验的候选身份和证据引用；
- 结果级验证失败时降级为 `inconclusive` 或回退递归分析。

## 7. 候选池与成本控制

在事实闭包完成后，将默认全局候选池限制为 24 至 32 个，并设置分层配额：

- authored root candidates；
- upstream propagation nodes；
- outcome evidence；
- exculpatory evidence；
- same-turn decision siblings。

共享 response、action group 和 artifact 内容只发送一次，候选胶囊通过稳定 ref
引用公共事实。裁剪不得成为不可恢复边界，Judge 可通过有界扩展请求被裁剪内容。

## 8. 错误处理

- Artifact 声明为 bundled 但无法读取：标记 Trace 完整性错误，不静默忽略。
- External fact revision 不匹配：保留记录但不得用于决定性判断。
- Claim fragment 无法组成完整 group：该 seed 输出 `evidence_gap`。
- Global Judge schema 校验失败：执行有限修复；仍失败则回退递归流程。
- LLM、网络或预算失败：保存 checkpoint，可恢复执行，不发布未经确认的根因。
- 单个 seed 失败：不得丢弃其他 seed 已完成的结果。

## 9. 测试与评估

### 9.1 单元与契约测试

- Claim 切分覆盖括号、逗号、代码片段、缩写和多句响应；
- Artifact bundle 复制后仍可解析，hash 与 byte range 校验正确；
- External evaluation fact 的来源和 revision 校验；
- Per-seed 状态互不覆盖；
- Evidence expansion 只能引用已知 anchor；
- Competitor coverage、防漂移和根因资格校验；
- 插装启用与禁用时 Agent 输入、输出和工具行为等价。

### 9.2 Benchmark 回归顺序

1. 先冻结 Axios、Astropy、TerminalBench 当前 Trace 和人工分析；
2. 完成事实层后重建 Causal IR，不重新运行 Agent，验证历史问题是否消失；
3. 使用最新版 observable-opencode 重跑相同 case；
4. 新增至少三类不同 Benchmark：代码修改、需求语义理解、长上下文或多 Agent；
5. 对每个 case 比较人工分析、旧归因和新归因；
6. 做消融实验，分别关闭 artifact、external fact、global comparison 和独立确认。

### 9.3 验收指标

| 指标 | 目标 |
|---|---:|
| 人工根因 Candidate Recall@24 | 100% |
| Claim 错误片段数 | 0 |
| 关键 Artifact 可解析率 | 不低于 99% |
| Per-seed 人工结论一致率 | 不低于 80% |
| 错误确认根因数量 | 0 |
| Trace 节点压缩率 | 不低于 90% |
| 因证据缺失导致的 inconclusive | 较当前下降至少 50% |

## 10. 实施顺序

### 阶段一：Trace 事实闭包

实现 claim group、安全原子化、artifact bundle 和 external evaluation fact。先在
已有三组 Trace 上重建并回归，确保事实问题被单独验证。

### 阶段二：根因确认稳定性

实现 per-seed outcome、候选对比矩阵、有界扩展、Top-K 独立确认和防漂移校验。

### 阶段三：成本收敛与扩展评测

实现候选分层配额和共享证据去重，再扩展更多开源 Benchmark，报告准确性、召回、
压缩率、LLM 请求数和 token 成本。

## 11. 完成定义

只有在以下条件同时满足时，本轮设计才视为完成：

- Trace 事实闭包和归因确认均通过单元测试；
- 现有归因测试无回归；
- 三组既有 Benchmark 完成前后对比；
- 新增 Benchmark 覆盖至少三种失败类型；
- 每个 seed 均输出可复核结论或明确证据缺口；
- 已证明插装没有改变 Agent 原始行为；
- 实验报告记录人工判断、模块判断、差异和下一轮问题。
