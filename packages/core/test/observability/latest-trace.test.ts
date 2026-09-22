import { afterEach, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { LLMEvent } from "@opencode-ai/llm"
import { materializeTrace } from "@opencode-ai/core/observability/trace-materializer"
import {
  activeLatestTrace,
  closeLatestTrace,
  openLatestTrace,
  recordLatestTrace,
  recordLLMEvent,
  recordPromptTrace,
  recordSessionTrace,
} from "@opencode-ai/core/observability/latest-trace"

const roots: string[] = []

afterEach(async () => {
  delete process.env.OPENCODE_CASE_TRACE
  delete process.env.OPENCODE_CASE_ID
  delete process.env.OPENCODE_CASE_TRACE_DIR
  await Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true })))
})

test("records an admitted prompt as an externalized semantic artifact", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-prompt-trace-"))
  roots.push(root)
  process.env.OPENCODE_CASE_TRACE = "1"
  process.env.OPENCODE_CASE_ID = "case_latest_prompt"
  process.env.OPENCODE_CASE_TRACE_DIR = root

  recordPromptTrace({
    sessionID: "ses_latest",
    messageID: "msg_latest",
    delivery: "queue",
    prompt: { text: "migrate the session runtime", parts: [{ type: "text" }] },
  })

  const result = materializeTrace({ caseDir: path.join(root, "case_latest_prompt") })
  expect(result.completeness).toBe("complete")
  const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))
  expect(trace.nodes).toEqual(expect.arrayContaining([expect.objectContaining({ kind: "prompt.assembly" })]))
  expect(trace.artifacts).toEqual(expect.arrayContaining([expect.objectContaining({ media_type: "application/json" })]))
})

test("records tool provenance and a resolvable call-to-result edge", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-tool-trace-"))
  roots.push(root)
  process.env.OPENCODE_CASE_TRACE = "1"
  process.env.OPENCODE_CASE_ID = "case_latest_tool"
  process.env.OPENCODE_CASE_TRACE_DIR = root

  const trace = openLatestTrace({ sessionID: "ses_latest", step: 1, model: "test/model" })!
  recordLatestTrace(trace, { operation: "llm.call", component: "llm", node_id: `llm_call_${trace.runID}` })
  recordLLMEvent(trace, LLMEvent.toolCall({ id: "call_latest", name: "skill", input: { name: "build" } }))
  recordLLMEvent(
    trace,
    LLMEvent.toolResult({
      id: "call_latest",
      name: "skill",
      result: { type: "text", value: "skill content" },
    }),
  )
  closeLatestTrace("ses_latest", trace, "completed")

  const result = materializeTrace({ caseDir: path.join(root, "case_latest_tool") })
  const document = JSON.parse(await fs.readFile(result.traceFile, "utf8"))
  expect(document.edges).toEqual(
    expect.arrayContaining([expect.objectContaining({ normalized_relation: "returned_by" })]),
  )
  expect(document.diagnostics).toEqual([])
})

test("routes semantic records to nested tool traces and resumes the parent after close", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-nested-trace-"))
  roots.push(root)
  process.env.OPENCODE_CASE_TRACE = "1"
  process.env.OPENCODE_CASE_ID = "case_latest_nested"
  process.env.OPENCODE_CASE_TRACE_DIR = root

  const parent = openLatestTrace({ sessionID: "ses_nested", step: 1 })!
  const child = openLatestTrace({ sessionID: "ses_nested", step: 2 })!
  expect(activeLatestTrace("ses_nested")?.runID).toBe(child.runID)
  recordSessionTrace("ses_nested", {
    operation: "skill.load",
    component: "skill",
    node_id: "nested_skill_load",
    data: { name: "build" },
  })
  closeLatestTrace("ses_nested", child, "completed")
  expect(activeLatestTrace("ses_nested")?.runID).toBe(parent.runID)
  recordSessionTrace("ses_nested", {
    operation: "agent.lifecycle",
    component: "processor",
    node_id: "parent_resumed",
    data: { phase: "after_child" },
  })
  closeLatestTrace("ses_nested", parent, "completed")

  const result = materializeTrace({ caseDir: path.join(root, "case_latest_nested") })
  const document = JSON.parse(await fs.readFile(result.traceFile, "utf8"))
  expect(document.nodes).toEqual(
    expect.arrayContaining([expect.objectContaining({ metadata: expect.objectContaining({ original_node_id: "nested_skill_load" }) })]),
  )
  expect(document.nodes).toEqual(
    expect.arrayContaining([expect.objectContaining({ metadata: expect.objectContaining({ original_node_id: "parent_resumed" }) })]),
  )
})
