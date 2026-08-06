# Resumable Judge Attribution 设计

## 目标

解决长链离线归因中 Provider 连续连接失败导致成功判断丢失、失败节点被批量降级为 unknown、重跑必须重复消耗全部请求的问题，同时补齐 Progress 导航窗口成员到窗口 anchor 的显式因果边。

## 约束

- 仅修改离线归因模块，不改变 Agent、LLM、工具、MCP、Skill、Trace 采集或 benchmark 执行行为。
- 缓存不保存 API key，不保存额外的 Agent 私域内容；只保存内容哈希、节点标识和已校验判断。
- 只有通过 schema 校验的节点判断和根因确认结果可以进入缓存。
- Provider/网络失败、超时和 schema repair exhausted 结果不得缓存。
- 缓存命中必须要求请求语义完全一致，不能仅按 node ref 复用。

## 方案比较

### 方案 A：只增加 SDK 重试

实现简单，但长时间 Provider 故障会继续阻塞，每次进程重启仍从头调用，不能复用前 61 个成功节点，不采用。

### 方案 B：内容寻址缓存、熔断与 Resume

以完整 Judge 请求语义生成 SHA-256 key，成功后立即 append 到 JSONL checkpoint。连续 Provider 连接错误达到阈值时停止当前缺陷分支并输出可恢复报告。重跑时只补齐 cache miss，作为本轮方案。

### 方案 C：集中式数据库任务队列

适合大规模分布式评测，但引入服务部署、并发锁和数据库依赖，超出当前单机 benchmark 归因范围，暂不采用。

## 组件设计

### JudgmentCache

新增独立 `cache.py`：

- 缓存 key 包含 stage、model、system prompt、messages/prompt、max tokens、thinking config 和 prompt schema version。
- JSONL 每条记录包含 cache version、key、stage、node ref、model、timestamp 和序列化 `NodeJudgment`。
- 加载时忽略损坏尾行并记录 corrupt count。
- 每次成功判断后 append、flush、fsync，保证异常退出后的最大丢失范围为当前请求。
- 同一个 key 后写覆盖内存索引，但文件保持追加审计历史。

### Provider Circuit Breaker

- 默认阈值为连续 3 次 Provider 连接类错误。
- 任意成功 API 请求将连续错误计数归零。
- 第 3 次错误抛出 `JudgeProviderUnavailable`。
- Analyzer 捕获后停止当前缺陷分支，保留 queue 和当前 ref 的 unresolved 状态，并将 termination reason 设置为 `provider_unavailable`。
- 不继续为后续节点制造 fallback unknown。

### Resume

- CLI 默认 checkpoint 为 `<attribution-out-stem>.judge-cache.jsonl`。
- 使用相同 trace、review、objective、model 和代码 prompt 版本重跑时自动命中缓存。
- 节点上下文、artifact、缺陷 fingerprint 或 prompt 变化会改变 key，自动重新判断。
- 报告记录 cache path、hits、misses、writes、loaded entries、corrupt entries 和 circuit 状态。

### Progress Window Edge

当 concrete candidate 通过 delivery-bounded navigation window 连接到非直属 Progress Episode anchor 时，Judge 上下文记录：

```json
{
  "relation": "progress_window_member",
  "evidence_type": "offline_reconstruction",
  "inference_method": "delivery_bounded_no_delivery_window_v1",
  "edge_origin": "offline.progress_navigation"
}
```

该边仅用于离线导航和解释，不声称缺陷传播。

## 错误处理

- 连接错误：计入熔断；达到阈值后分支立即停止。
- 单次超时：视为 Provider 请求失败并计入熔断。
- schema invalid：沿用 repair/retry，不计入连接熔断；repair exhausted 不缓存。
- 缓存损坏：跳过损坏行，继续使用其他记录，并在报告中暴露。
- cache 写失败：不中断已经得到的判断，但报告 cache write error；本轮不反馈 Agent。

## 验收标准

1. 相同请求第二次执行不调用 Provider，返回同一 `NodeJudgment`。
2. objective、上下文、模型或 prompt version 任一变化都会 cache miss。
3. 连续 3 次连接错误后 Analyzer 终止分支，后续节点不再调用 Judge。
4. Resume 复用成功节点，只重新调用上次失败及未访问节点。
5. Sphinx `dec_85` 上下文使用 `progress_window_member`，不再使用通用 `backward_path_predecessor`。
6. 完整归因单测通过；实际 Sphinx 重跑的重复请求量显著低于无缓存重跑。
