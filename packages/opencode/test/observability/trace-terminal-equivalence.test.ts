import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"

const packageDir = path.resolve(import.meta.dir, "../..")
const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
const materializerModule = pathToFileURL(path.join(packageDir, "src/observability/trace-materializer.ts")).href

function summarizedText(value: unknown) {
  if (typeof value === "string") return value
  if (value && typeof value === "object" && "preview" in value) return (value as { preview?: unknown }).preview
  return undefined
}

function signature(trace: any, legacy: any) {
  const terminalKinds = new Set([
    "agent.lifecycle",
    "response.output",
    "response.claim",
    "claim.support_assessment",
    "case.completed",
  ])
  return {
    manifest: {
      status: trace.manifest.status,
      server_status: trace.manifest.server_status,
      process_status: trace.manifest.process_status,
      case_status: trace.manifest.case_status,
      collection_mode: trace.manifest.collection_mode,
      behavior_impact: trace.manifest.behavior_impact,
      input: trace.manifest.input,
      result: trace.manifest.result,
    },
    terminal_nodes: trace.nodes
      .filter((node: any) => terminalKinds.has(node.kind))
      .map((node: any) => ({
        kind: node.kind,
        status: node.status,
        finalized_status: node.data?.finalized_status,
      })),
    constraints: legacy.constraint_records.map((item: any) => ({
      source: item.source,
      constraint: item.constraint,
      status: item.status,
    })),
    responses: legacy.response_segments.map((item: any) => ({
      text: summarizedText(item.text),
      response_role: item.response_role,
      is_final_for_case: item.is_final_for_case,
    })),
    spans: legacy.spans.map((item: any) => ({
      component: item.component,
      operation: item.operation,
      status: item.status,
      finalized_status: item.metadata?.finalized_status,
    })),
  }
}

test("journal-only terminal materialization is semantically equivalent to deterministic full finish", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-terminal-equivalence-"))
  const script = path.join(root, "terminal-equivalence.ts")
  try {
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `import { materializeTrace } from ${JSON.stringify(materializerModule)}`,
        `const trace = CaseTrace.configure({ input: { prompt: "equivalence" }, environment: { model: "fixture" } }) as any`,
        `CaseTrace.setSessionID("ses_equivalence")`,
        `trace.startSpan({ component: "tool", operation: "read", name: "open span" })`,
        `CaseTrace.constraint({ source: "user", constraint: "do not edit", status: "observed_satisfied" })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_equivalence", message_id: "msg", agent: "build", phase: "turn.started", status: "running", summary: "open lifecycle" })`,
        `CaseTrace.responseOutput({ text: "equivalent answer", response_role: "final_answer", visibility: "user_visible", is_final_for_case: true, finality_source: "explicit" })`,
        `if (process.env.TRACE_TERMINAL_MODE === "full") trace.finish({ status: "success", result: { answer: "equivalent answer" } })`,
        `else { const request = CaseTrace.closeAll({ status: "success", result: { answer: "equivalent answer" } })[0]; materializeTrace({ caseDir: request.caseDir }) }`,
      ].join("\n"),
    )

    const outputs: any[] = []
    for (const mode of ["full", "journal"] as const) {
      const traceRoot = path.join(root, mode)
      const child = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_TRACE_DIR: traceRoot,
          OPENCODE_CASE_ID: `terminal-equivalence-${mode}`,
          OPENCODE_CASE_TRACE_QUIET: "1",
          TRACE_TERMINAL_MODE: mode,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const [exitCode, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()])
      expect(exitCode, stderr).toBe(0)
      const caseDir = path.join(traceRoot, `terminal-equivalence-${mode}`)
      outputs.push({
        trace: JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")),
        legacy: JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")),
      })
    }

    expect(signature(outputs[1].trace, outputs[1].legacy)).toEqual(signature(outputs[0].trace, outputs[0].legacy))
  } finally {
    await fs.rm(root, { recursive: true, force: true })
  }
})
