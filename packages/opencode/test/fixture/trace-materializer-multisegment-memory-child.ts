import fs from "node:fs"
import path from "node:path"

const NODE_COUNT = 31_000
const caseDir = process.env.OPENCODE_TRACE_MATERIALIZER_MULTI_MEMORY_DIR!
const { materializeTrace } = await import("@/observability/trace-materializer")
const journalBytes = JSON.parse(fs.readFileSync(path.join(caseDir, "session.json"), "utf8")).segments.reduce(
  (total: number, segment: { records: string }) => total + fs.statSync(path.join(caseDir, segment.records)).size,
  0,
)

Bun.gc(true)
const beforeRSS = process.memoryUsage().rss
const result = materializeTrace({ caseDir })
Bun.gc(true)
const afterRSS = process.memoryUsage().rss
const maxRSS = process.resourceUsage().maxRSS
const trace = JSON.parse(fs.readFileSync(result.traceFile, "utf8")) as any
const referenceNodes = trace.nodes.filter((node: any) => node.kind === "response.output" && node.title === "alias ref")

if (result.completeness !== "complete" || result.recoveredLines !== 2 * (NODE_COUNT + 4))
  throw new Error(`unexpected materialization result: ${JSON.stringify(result)}`)
if (
  trace.manifest.case_id !== "multi-segment-high-cardinality" ||
  trace.manifest.segment_summary?.count !== 2 ||
  trace.manifest.segment_summary?.completed !== 2
)
  throw new Error(`unexpected materialized manifest: ${JSON.stringify(trace.manifest)}`)
const resolvedRefs = referenceNodes.map((node: any) => node.input_refs[0].ref_id)
if (
  JSON.stringify(resolvedRefs) !==
  JSON.stringify(["run_high_cardinality_1::node::node_030999", "run_high_cardinality_2::node::node_030999"])
)
  throw new Error(`unexpected namespaced aliases: ${JSON.stringify(resolvedRefs)}`)

process.stdout.write(
  `\nTRACE_MATERIALIZER_MULTI_MEMORY ${JSON.stringify({ journalBytes, beforeRSS, afterRSS, maxRSS })}\n`,
)
