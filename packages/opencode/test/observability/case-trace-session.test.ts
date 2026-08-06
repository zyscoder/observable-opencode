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

test("preserves an already-resolved child as an orphan when aliasing it to a parent", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const child = registry.resolve({ sessionID: "ses_child" })
  registry.remember(child, ["span:child_span"])

  const parent = registry.alias("ses_child", "ses_parent")

  expect(registry.resolve({ sessionID: "ses_child" })).toBe(parent)
  expect(registry.resolve({ refs: ["span:child_span"] })).toBe(child)
  expect(registry.values()).toEqual([parent, child])

  registry.finishAll((trace) => trace.finished.push({ status: "success" }))

  expect(parent.finished).toEqual([{ status: "success" }])
  expect(child.finished).toEqual([{ status: "success" }])
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

test("continues finalization after a failure and retries the failed trace", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const first = registry.resolve({ sessionID: "ses_first" })
  const second = registry.resolve({ sessionID: "ses_second" })

  expect(() =>
    registry.finishAll((trace) => {
      if (trace === first) throw new Error("first finalizer failed")
      trace.finished.push({ status: "success" })
    }),
  ).toThrow(AggregateError)
  expect(second.finished).toEqual([{ status: "success" }])

  registry.finishAll((trace) => trace.finished.push({ status: "success" }))

  expect(first.finished).toEqual([{ status: "success" }])
  expect(second.finished).toEqual([{ status: "success" }])
})

test("clears routing state after a failed reset and starts a fresh lifecycle", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  registry.resolve({ sessionID: "ses_failed" })

  expect(() => registry.reset(() => {
    throw new Error("reset finalizer failed")
  })).toThrow(AggregateError)
  expect(registry.values()).toEqual([])

  const fresh = registry.resolve({ sessionID: "ses_fresh" })
  registry.finishAll((trace) => trace.finished.push({ status: "success" }))

  expect(fresh.finished).toEqual([{ status: "success" }])
})

test("extracts route hints from session and reference fields", () => {
  expect(traceRouteHint({ sessionID: "ses_direct" })).toEqual({ sessionID: "ses_direct", refs: [] })
  expect(traceRouteHint({ session_id: "ses_snake" })).toEqual({ sessionID: "ses_snake", refs: [] })
  expect(traceRouteHint({ input: { sessionID: "ses_input" } })).toEqual({ sessionID: "ses_input", refs: [] })
  expect(traceRouteHint({ data: { sessionID: "ses_data" } })).toEqual({ sessionID: "ses_data", refs: [] })
  expect(traceRouteHint({ metadata: { sessionID: "ses_metadata" } })).toEqual({ sessionID: "ses_metadata", refs: [] })
  expect(traceRouteHint({ span_id: "span_1", source_refs: ["decision:dec_1"] })).toEqual({
    refs: ["span:span_1", "decision:dec_1"],
  })
})

test("does not collect session IDs below the accepted routing containers", () => {
  expect(
    traceRouteHint({ data: { child_session_id: "ses_child", message: { sessionID: "ses_child" } } }),
  ).toEqual({ refs: [] })
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
