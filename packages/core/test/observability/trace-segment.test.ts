import { afterEach, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { openTraceSegment, readTraceSessionManifest } from "@opencode-ai/core/observability/trace-segment"

const roots: string[] = []

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true })))
})

test("opens an append-only segment and persists its manifest", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-trace-"))
  roots.push(root)

  const segment = openTraceSegment({
    rootDir: root,
    logicalCaseID: "case_latest",
    sessionID: "ses_latest",
    runID: "run_latest",
  })

  const recordsFile = path.join(segment.logicalRoot, segment.descriptor.records)
  await fs.appendFile(recordsFile, '{"operation":"run.start"}\n')
  expect(segment.finalize("completed")).toBe(true)

  const manifest = readTraceSessionManifest(segment.sessionFile)
  expect(manifest).toBeDefined()
  if (!manifest) return
  expect(manifest.logical_case_id).toBe("case_latest")
  expect(manifest.session_id).toBe("ses_latest")
  expect(manifest.segments).toHaveLength(1)
  expect(manifest.segments[0]).toMatchObject({
    run_id: "run_latest",
    status: "completed",
  })
  expect(await fs.readFile(recordsFile, "utf8")).toContain('"run.start"')
})

test("creates a continuation segment without rewriting the previous segment", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-continuation-"))
  roots.push(root)

  const first = openTraceSegment({
    rootDir: root,
    logicalCaseID: "case_continue",
    sessionID: "ses_continue",
    runID: "run_first",
  })
  const firstRecordsFile = path.join(first.logicalRoot, first.descriptor.records)
  await fs.appendFile(firstRecordsFile, '{"operation":"run.start","run_id":"run_first"}\n')
  first.finalize("completed")

  const second = openTraceSegment({
    rootDir: root,
    logicalCaseID: "case_continue",
    sessionID: "ses_continue",
    runID: "run_second",
  })
  const manifest = readTraceSessionManifest(second.sessionFile)
  expect(manifest).toBeDefined()
  if (!manifest) return

  expect(manifest.segments).toHaveLength(2)
  expect(manifest.segments[1]).toMatchObject({
    run_id: "run_second",
    continuation_of: "run_first",
  })
  expect(await fs.readFile(firstRecordsFile, "utf8")).toContain('"run_first"')
})

test("keeps parent and subagent sessions in one logical case trace", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-subagent-case-"))
  roots.push(root)

  const parent = openTraceSegment({
    rootDir: root,
    logicalCaseID: "case_subagent",
    sessionID: "ses_parent",
    runID: "run_parent",
  })
  parent.finalize("completed")

  const child = openTraceSegment({
    rootDir: root,
    logicalCaseID: "case_subagent",
    sessionID: "ses_child",
    runID: "run_child",
  })
  expect(child.descriptor.session_id).toBe("ses_child")
  child.finalize("completed")

  const manifest = readTraceSessionManifest(child.sessionFile)
  expect(manifest?.session_id).toBe("ses_parent")
  expect(manifest?.segments.map((segment) => segment.session_id)).toEqual(["ses_parent", "ses_child"])
})
