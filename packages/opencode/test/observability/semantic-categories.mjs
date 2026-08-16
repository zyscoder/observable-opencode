import fs from "node:fs"

function semanticEntries(trace) {
  return [
    ...(Array.isArray(trace.records) ? trace.records : []),
    ...(Array.isArray(trace.nodes) ? trace.nodes : []),
  ]
}

export function collectTraceSemanticCategories(trace) {
  const categories = new Set()
  if (Array.isArray(trace.nodes) && trace.nodes.length) categories.add("node")
  if (
    (Array.isArray(trace.edges) && trace.edges.length) ||
    (Array.isArray(trace.dataflow_edges) && trace.dataflow_edges.length)
  )
    categories.add("edge")
  if (Array.isArray(trace.artifacts) && trace.artifacts.length) categories.add("artifact")

  for (const entry of semanticEntries(trace)) {
    const type = String(entry?.event_type ?? entry?.kind ?? "").toLowerCase()
    const component = String(entry?.component ?? "").toLowerCase()
    if (
      type === "run.start" ||
      type === "process.signal" ||
      type === "agent.lifecycle" ||
      type.startsWith("case.") ||
      type.startsWith("segment.")
    )
      categories.add("lifecycle")
    if (type.startsWith("context.compaction")) categories.add("compaction")
    if (type.startsWith("tool.") || component === "tool") categories.add("tool")
    if (type.startsWith("skill.") || component === "skill") categories.add("skill")
    if (type.startsWith("mcp.") || component === "mcp") categories.add("mcp")
    if (type.startsWith("subagent.") || (component === "task" && type.includes("subagent"))) categories.add("subagent")
    if (type.startsWith("response.")) categories.add("response")
  }
  return [...categories].sort()
}

export function assertTraceSemanticCategories(traceFile, required) {
  const categories = collectTraceSemanticCategories(JSON.parse(fs.readFileSync(traceFile, "utf8")))
  const missing = required.filter((category) => !categories.includes(category))
  if (missing.length) {
    throw new Error(
      `${traceFile}: missing trace semantic categories: ${missing.join(", ")}; observed: ${categories.join(", ") || "none"}`,
    )
  }
  return categories
}
