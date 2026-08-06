import { expect, test } from "bun:test"
import { SessionTraceRegistry, traceRouteHint } from "@/observability/case-trace-session"

type FakeTrace = {
  id: string
  finished: Array<{ status: string }>
  signals: NodeJS.Signals[]
}

const create = (id: string): FakeTrace => ({ id, finished: [], signals: [] })

test("isolates independent root sessions", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))

  expect(registry.resolve({ sessionID: "ses_a" })).not.toBe(registry.resolve({ sessionID: "ses_b" }))
  expect(registry.resolve({ sessionID: "ses_a" })).toBe(registry.resolve({ sessionID: "ses_a" }))
})

test("routes child sessions to their parent root", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const parent = registry.resolve({ sessionID: "ses_parent" })

  registry.alias("ses_child", "ses_parent")

  expect(registry.resolve({ sessionID: "ses_child" })).toBe(parent)
  expect(registry.values()).toEqual([parent])
})

test("routes reference-only records to their owner", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const owner = registry.resolve({ sessionID: "ses_owner" })

  registry.remember(owner, ["span:span_1", "decision:dec_1"])

  expect(registry.resolve({ refs: ["span:span_1"] })).toBe(owner)
})

test("finishes every root exactly once", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))

  registry.resolve({ sessionID: "ses_a" })
  registry.resolve({ sessionID: "ses_b" })
  registry.finishAll((trace) => trace.finished.push({ status: "success" }))
  registry.finishAll((trace) => trace.finished.push({ status: "success" }))

  expect(registry.values().map((trace) => trace.finished.length)).toEqual([1, 1])
})

test("extracts route hints from session and reference fields", () => {
  expect(traceRouteHint({ sessionID: "ses_direct" })).toEqual({ sessionID: "ses_direct", refs: [] })
  expect(traceRouteHint({ session_id: "ses_snake" })).toEqual({ sessionID: "ses_snake", refs: [] })
  expect(traceRouteHint({ data: { sessionID: "ses_data" } })).toEqual({ sessionID: "ses_data", refs: [] })
  expect(traceRouteHint({ metadata: { sessionID: "ses_metadata" } })).toEqual({ sessionID: "ses_metadata", refs: [] })
  expect(traceRouteHint({ span_id: "span_1", source_refs: ["decision:dec_1"] })).toEqual({
    refs: ["span:span_1", "decision:dec_1"],
  })
})

test("extracts edge endpoint references", () => {
  expect(
    traceRouteHint({
      from: { type: "span", id: "span_from" },
      to: { type: "node", id: "node_to" },
    }),
  ).toEqual({ refs: ["span:span_from", "node:node_to"] })
})

test("does not promote child_session_id to a root before aliasing", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const hint = traceRouteHint({ child_session_id: "ses_child" })

  expect(hint).toEqual({ refs: [] })
  expect(registry.resolve(hint).id).toBe("process")

  const parent = registry.alias("ses_child", "ses_parent")
  expect(registry.resolve({ sessionID: "ses_child" })).toBe(parent)
  expect(registry.values()).toEqual([parent, expect.objectContaining({ id: "process" })])
})
