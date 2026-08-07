import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import * as CausalIRModule from "@/observability/causal-ir"
import { replayCausalIRJournal } from "@/observability/causal-ir"
import { renderCaseTraceHtml } from "@/observability/case-trace-html"
import { renderProvenanceTraceHtml } from "@/observability/causal-trace-viewer"
import type { ProvenanceTraceSummary, TraceSummary } from "@/observability/case-trace"

process.env.OPENCODE_CASE_TRACE_QUIET = "1"

async function exists(file: string) {
  return fs
    .access(file)
    .then(() => true)
    .catch(() => false)
}

function routedIdentityDigestForTest(kind: "root" | "process" | "compatibility", sessionID?: string) {
  return createHash("sha256")
    .update(JSON.stringify({ kind, sessionID: sessionID ?? null }))
    .digest("hex")
    .slice(0, 12)
}

function routedCaseDirectoryForTest(base: string, kind: "root" | "process" | "compatibility", sessionID?: string) {
  const readable = kind === "root" ? sessionID!.replace(/[^a-zA-Z0-9._-]+/g, "_") : kind
  return `${base}--${readable}--${routedIdentityDigestForTest(kind, sessionID)}`
}

async function waitForExists(file: string, timeoutMs = 2000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await exists(file)) return true
    await Bun.sleep(50)
  }
  return exists(file)
}

function assertFinalCancelledPartialMatchesTrace(partial: any, trace: any) {
  expect(Object.keys(partial).sort()).toEqual([
    "artifacts",
    "causal_ir_version",
    "compatibility",
    "dataflow_edges",
    "diagnostics",
    "edges",
    "journal",
    "manifest",
    "metrics",
    "nodes",
    "records",
    "trace_version",
  ])
  expect(partial.trace_version).toBe("6.0")
  expect(partial.causal_ir_version).toBe("1.0")
  expect(partial.nodes).toEqual(trace.nodes)
  expect(partial.edges).toEqual(trace.edges)
  expect(partial.records).toEqual(trace.records)
  expect(partial.dataflow_edges).toEqual(trace.dataflow_edges)
  expect(partial.artifacts).toEqual(trace.artifacts)
  expect(partial.diagnostics).toEqual(trace.diagnostics)
  expect(partial.compatibility).toEqual(trace.compatibility)
  expect(partial.metrics).toEqual(trace.metrics)
  expect(partial.manifest).toEqual(trace.manifest)
  expect(partial.metrics.records).toBe(partial.nodes.length)
  expect(partial.metrics.dataflow_edges).toBe(partial.edges.length)
  expect(partial.metrics.artifacts).toBe(partial.artifacts.length)
  expect(partial.manifest.status).toBe("cancelled")
  expect(partial.manifest.server_status).toBe("cancelled")
  expect(partial.manifest.process_status).toBe("cancelled")
  expect(partial.manifest.case_status).toBe("cancelled")

  const lifecycle = partial.records.find((record: any) => record.event_type === "agent.lifecycle")
  const caseRecord = partial.records.find((record: any) => record.event_type === "case.failed")

  expect(lifecycle).toMatchObject({
    status: "cancelled",
    data: {
      finalized_status: "finalized_without_close",
      finalized_reason: "trace_cancelled",
    },
  })
  expect(caseRecord).toMatchObject({
    status: "cancelled",
    data: {
      case_status: "cancelled",
      server_status: "cancelled",
      process_status: "cancelled",
    },
  })
}

function expectResponseClaimAtomizationClosure(value: any) {
  expect(value).toEqual(
    expect.objectContaining({
      claim_group_id: expect.any(String),
      claim_count: expect.any(Number),
      source_byte_range: [expect.any(Number), expect.any(Number)],
      atomization_status: expect.any(String),
      atomization_reason: expect.any(String),
    }),
  )
  expect(value.claim_group_id.length).toBeGreaterThan(0)
  expect(value.claim_count).toBeGreaterThan(0)
  expect(value.source_byte_range[0]).toBeGreaterThanOrEqual(0)
  expect(value.source_byte_range[1]).toBeGreaterThanOrEqual(value.source_byte_range[0])
  expect(["atomic", "group_required", "invalid_fragment"]).toContain(value.atomization_status)
  expect(value.atomization_reason.length).toBeGreaterThan(0)
}

function changeIdFromTrace(trace: any) {
  const record = trace.records.find((item: any) => item.event_type === "change")
  return record?.data?.change_id ?? record?.record_id
}

function temporalAdvisoryEdgesTo(trace: any, targetID: string) {
  return trace.edges.filter(
    (edge: any) => edge.to?.ref_id === targetID && edge.derivation_method === "recent_source_fallback",
  )
}

function contextSetMemberRefs(trace: any, edge: any) {
  const contextSet = trace.records.find((record: any) => record.record_id === edge.from?.ref_id)
  return contextSet?.data?.member_refs ?? []
}

function temporalAdvisoryMemberRefs(trace: any, targetID: string) {
  return [
    ...new Set(temporalAdvisoryEdgesTo(trace, targetID).flatMap((edge: any) => contextSetMemberRefs(trace, edge))),
  ]
}

function temporalAdvisoryEdgesForMember(trace: any, memberRef: string) {
  const contextSetIDs = new Set(
    trace.records
      .filter(
        (record: any) =>
          record.event_type === "context.pack" &&
          record.data?.context_set_kind === "temporal_advisory" &&
          record.data?.member_refs?.includes(memberRef),
      )
      .map((record: any) => record.record_id),
  )
  return trace.edges.filter(
    (edge: any) => contextSetIDs.has(edge.from?.ref_id) && edge.derivation_method === "recent_source_fallback",
  )
}

const causalIRJournalContract = {
  "node.created": { recordType: "node", category: "node", startsChain: true },
  "node.updated": { recordType: "node.update", category: "node", requiresPrevious: true },
  "edge.created": { recordType: "edge", category: "edge", startsChain: true },
  "artifact.created": { recordType: "artifact", category: "artifact", startsChain: true },
  "artifact.reused": { recordType: "artifact.reuse", category: "artifact", requiresPrevious: true },
  "diagnostic.created": { recordType: "diagnostic", category: "diagnostic", startsChain: true },
  "case.checkpointed": { recordType: "checkpoint", category: "case" },
  // Finalization continues an existing checkpoint chain when one exists, but can be the first lifecycle record.
  "case.finalized": { recordType: "finish", category: "case" },
} as const

function canonicalJSONForCausalIRAudit(input: unknown, arrayValue = false): string | undefined {
  if (input === null) return "null"

  switch (typeof input) {
    case "boolean":
    case "number":
    case "string":
      return JSON.stringify(input)
    case "undefined":
    case "function":
    case "symbol":
      return arrayValue ? "null" : undefined
    case "bigint":
      throw new TypeError("Do not know how to serialize a BigInt")
  }

  if (Array.isArray(input)) {
    const values: string[] = []
    for (let index = 0; index < input.length; index++)
      values.push(canonicalJSONForCausalIRAudit(input[index], true) ?? "null")
    return `[${values.join(",")}]`
  }

  const value = input as Record<string, unknown>
  if (typeof value.toJSON === "function") return canonicalJSONForCausalIRAudit(value.toJSON(), arrayValue)
  return `{${Object.keys(value)
    .sort((left, right) => (left === right ? 0 : left < right ? -1 : 1))
    .flatMap((key) => {
      const serialized = canonicalJSONForCausalIRAudit(value[key])
      return serialized === undefined ? [] : [`${JSON.stringify(key)}:${serialized}`]
    })
    .join(",")}}`
}

function causalIRPayloadHashForAudit(data: unknown) {
  return createHash("sha256")
    .update(canonicalJSONForCausalIRAudit(data) ?? "null")
    .digest("hex")
}

function assertCausalIRJournalAudit(journal: unknown[]) {
  const previousPayloadHashes = new Map<string, string>()

  for (const [index, rawEntry] of journal.entries()) {
    if (!rawEntry || typeof rawEntry !== "object" || Array.isArray(rawEntry))
      throw new Error(`journal entry ${index + 1} is not an object`)

    const entry = rawEntry as Record<string, unknown>
    const operation = entry.operation
    if (typeof operation !== "string" || !(operation in causalIRJournalContract))
      throw new Error(`journal entry ${index + 1} has an unsupported operation`)

    const contract = causalIRJournalContract[operation as keyof typeof causalIRJournalContract]
    if (entry.sequence !== index + 1) throw new Error(`journal entry ${index + 1} has a non-contiguous sequence`)
    if (entry.record_type !== contract.recordType)
      throw new Error(`journal entry ${index + 1} has an invalid record_type for ${operation}`)
    if (typeof entry.entity_id !== "string") throw new Error(`journal entry ${index + 1} is missing an entity_id`)

    const payloadHash = causalIRPayloadHashForAudit(entry.data)
    if (entry.payload_hash !== payloadHash) throw new Error(`journal entry ${index + 1} has an invalid payload_hash`)

    const chainKey = `${contract.category}:${entry.entity_id}`
    const expectedPreviousHash = previousPayloadHashes.get(chainKey)
    if (entry.previous_payload_hash !== expectedPreviousHash)
      throw new Error(`journal entry ${index + 1} has an invalid previous_payload_hash chain`)
    if ("startsChain" in contract && contract.startsChain && expectedPreviousHash !== undefined)
      throw new Error(`journal entry ${index + 1} must start a ${contract.category} payload chain`)
    if ("requiresPrevious" in contract && contract.requiresPrevious && expectedPreviousHash === undefined)
      throw new Error(`journal entry ${index + 1} must continue a ${contract.category} payload chain`)

    previousPayloadHashes.set(chainKey, payloadHash)
  }
}

async function readCausalIRJournal(caseDir: string) {
  return (await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8"))
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line))
}

function isCompleteCausalIRCheckpoint(entry: any) {
  const snapshot = entry?.data?.snapshot
  return (
    entry?.operation === "case.checkpointed" &&
    snapshot &&
    typeof snapshot === "object" &&
    Array.isArray(snapshot.nodes) &&
    Array.isArray(snapshot.edges) &&
    Array.isArray(snapshot.artifacts) &&
    Array.isArray(snapshot.diagnostics)
  )
}

function checkpointIncludesNode(entry: any, markerNodeID: string) {
  return (
    isCompleteCausalIRCheckpoint(entry) && entry.data.snapshot.nodes.some((node: any) => node?.node_id === markerNodeID)
  )
}

async function waitForCompleteCausalIRCheckpoint(caseDir: string, markerNodeID: string, timeoutMs = 3000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    try {
      const journal = await readCausalIRJournal(caseDir)
      if (journal.some((entry) => checkpointIncludesNode(entry, markerNodeID))) return journal
    } catch {}
    await Bun.sleep(50)
  }

  try {
    const journal = await readCausalIRJournal(caseDir)
    return journal.some((entry) => checkpointIncludesNode(entry, markerNodeID)) ? journal : undefined
  } catch {
    return undefined
  }
}

function assertJournalReplaysCanonicalTrace(journal: unknown[], trace: any) {
  const replayed = replayCausalIRJournal(journal)
  expect(replayed.nodes.map((node) => node.node_id)).toEqual(trace.nodes.map((node: any) => node.node_id))
  expect(replayed.edges.map((edge) => edge.edge_id)).toEqual(trace.edges.map((edge: any) => edge.edge_id))
  expect(replayed.artifacts.map((artifact) => [artifact.artifact_id, artifact.hash])).toEqual(
    trace.artifacts.map((artifact: any) => [artifact.artifact_id, artifact.hash]),
  )
  expect(replayed.diagnostics).toEqual(trace.diagnostics)
  expect(replayed).toMatchObject({
    nodes: trace.nodes,
    edges: trace.edges,
    artifacts: trace.artifacts,
  })
  const replayTrace = (CausalIRModule as any).replayCausalIRTrace
  expect(typeof replayTrace).toBe("function")
  if (typeof replayTrace === "function") expect(replayTrace(journal)).toEqual(trace)
}

function assertExactlyOneFinalizationAtEnd(journal: any[]) {
  expect(journal.filter((entry) => entry.operation === "case.finalized")).toHaveLength(1)
  expect(journal.at(-1)?.operation).toBe("case.finalized")
}

function assertFinalForcedCheckpointMatchesCanonicalTrace(journal: any[], partial: any, trace: any) {
  assertCausalIRJournalAudit(journal)
  assertExactlyOneFinalizationAtEnd(journal)

  const finalized = journal.at(-1)
  expect(finalized?.payload_hash).toBe(causalIRPayloadHashForAudit(finalized?.data))
  expect(finalized?.data?.format).toBe("compact_causal_ir_finalization")
  expect(finalized?.data?.snapshot).toBeUndefined()
  expect(finalized?.data?.trace).toBeUndefined()
  expect(finalized?.data?.data).toEqual(partial.manifest)
  expect(finalized?.data?.graph).toMatchObject({
    nodes: trace.nodes.length,
    edges: trace.edges.length,
    artifacts: trace.artifacts.length,
    diagnostics: trace.diagnostics.length,
  })
  expect(partial).toEqual(trace)
}

describe("case trace", () => {
  test("isolates root sessions and keeps child aliases in the parent trace", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-session-routing-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "session-routing.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_root_a", message_id: "msg_a", input: { parts: [{ type: "text", text: "root A" }] } })`,
        `const spanA = "startSpan" in CaseTrace ? CaseTrace.startSpan({ component: "llm", operation: "stream", name: "compatible/model-a", input: { sessionID: "ses_root_a" } }) : CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "compatible/model-a", input: { sessionID: "ses_root_a" } })`,
        `if ("aliasSession" in CaseTrace) CaseTrace.aliasSession("ses_child_a", "ses_root_a")`,
        `CaseTrace.llmTurn({ turn_id: "turn_child_a", session_id: "ses_child_a", parent_session_id: "ses_root_a", agent: "general", agent_role: "subagent", provider_id: "compatible", model_id: "model-a", status: "success", source_refs: spanA ? ["span:" + spanA.id] : [] })`,
        `CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_root_b", message_id: "msg_b", input: { parts: [{ type: "text", text: "root B" }] } })`,
        `if ("finishAll" in CaseTrace) CaseTrace.finishAll({ status: "success", result: { reason: "test.shutdown" } }); else CaseTrace.finish({ status: "success", result: { reason: "test.shutdown" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "multi-session-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDirectories = (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
    const rootBCaseDirectory = routedCaseDirectoryForTest("multi-session-case", "root", "ses_root_b")
    expect(caseDirectories).toEqual(["multi-session-case", rootBCaseDirectory])

    const rootADir = path.join(dir, "multi-session-case")
    const rootBDir = path.join(dir, rootBCaseDirectory)
    const manifestA = JSON.parse(await fs.readFile(path.join(rootADir, "manifest.json"), "utf8")) as any
    const manifestB = JSON.parse(await fs.readFile(path.join(rootBDir, "manifest.json"), "utf8")) as any
    const traceA = await fs.readFile(path.join(rootADir, "trace.json"), "utf8")
    const traceB = await fs.readFile(path.join(rootBDir, "trace.json"), "utf8")

    expect(manifestA.session_id).toBe("ses_root_a")
    expect(manifestB.session_id).toBe("ses_root_b")
    expect(manifestA.run_id).not.toBe(manifestB.run_id)
    expect(traceA).toContain("ses_root_a")
    expect(traceA).toContain("ses_child_a")
    expect(traceA).toContain("root A")
    expect(traceA).not.toContain("root B")
    expect(traceB).toContain("ses_root_b")
    expect(traceB).not.toContain("ses_root_a")
    expect(traceB).not.toContain("ses_child_a")
    expect(traceB).toContain("root B")
    expect(traceB).not.toContain("root A")
  })

  test("claims the compatibility trace once and keeps later roots independent", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-compatibility-identity-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compatibility-identity.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const configured = CaseTrace.configure({ caseID: "compatibility-identity-case" })`,
        `CaseTrace.setSessionID("ses_a")`,
        `CaseTrace.promptAssembly({ stage: "root_a", session_id: "ses_a", input: { text: "prompt A" } })`,
        `CaseTrace.setSessionID("ses_b")`,
        `CaseTrace.promptAssembly({ stage: "root_b", session_id: "ses_b", input: { text: "prompt B" } })`,
        `const unscoped = CaseTrace.get()`,
        `CaseTrace.finishAll({ status: "success" })`,
        `process.stdout.write(JSON.stringify({ configuredCaseID: configured?.caseID, unscopedCaseID: unscoped?.caseID, same: configured === unscoped }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    const processCaseDirectory = routedCaseDirectoryForTest("compatibility-identity-case", "process")
    const rootBCaseDirectory = routedCaseDirectoryForTest("compatibility-identity-case", "root", "ses_b")
    expect(JSON.parse(await new Response(proc.stdout).text())).toEqual({
      configuredCaseID: "compatibility-identity-case",
      unscopedCaseID: processCaseDirectory,
      same: false,
    })

    const caseDirectories = (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
    expect(caseDirectories).toEqual([
      "compatibility-identity-case",
      processCaseDirectory,
      rootBCaseDirectory,
    ])

    const traces = await Promise.all(
      caseDirectories.map(async (caseDirectory) => ({
        caseDirectory,
        trace: JSON.parse(await fs.readFile(path.join(dir, caseDirectory, "trace.json"), "utf8")) as any,
      })),
    )
    const rootA = traces.find((item) => item.trace.manifest.session_id === "ses_a")!
    const rootB = traces.find((item) => item.trace.manifest.session_id === "ses_b")!
    const processTrace = traces.find((item) => item.trace.manifest.session_id === undefined)!

    expect(rootA.caseDirectory).toBe("compatibility-identity-case")
    expect(rootA.trace.records.some((record: any) => record.data?.stage === "root_a")).toBe(true)
    expect(rootA.trace.records.some((record: any) => record.data?.stage === "root_b")).toBe(false)
    expect(rootB.trace.records.some((record: any) => record.data?.stage === "root_b")).toBe(true)
    expect(rootB.trace.records.some((record: any) => record.data?.stage === "root_a")).toBe(false)
    expect(processTrace.caseDirectory).toBe(processCaseDirectory)
  })

  test("keeps rootless and unknown-ref records in an isolated process trace", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-rootless-routing-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "rootless-routing.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const promptA = CaseTrace.promptAssembly({ stage: "root_a", session_id: "ses_root_a", input: { text: "root A" } })`,
        `CaseTrace.promptAssembly({ stage: "root_b", session_id: "ses_root_b", input: { text: "root B" } })`,
        `CaseTrace.event({ component: "trace", event_type: "rootless.marker", data: { marker: "rootless only" } })`,
        `CaseTrace.event({ component: "trace", event_type: "unknown.marker", data: { marker: "unknown only", source_refs: ["span:unknown"] } })`,
        `CaseTrace.event({ component: "trace", event_type: "owner.marker", data: { marker: "owner only", source_refs: ["node:" + promptA?.node_id] } })`,
        `CaseTrace.finishAll({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "rootless-routing-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDirectories = (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
    const processCaseDirectory = routedCaseDirectoryForTest("rootless-routing-case", "process")
    const rootBCaseDirectory = routedCaseDirectoryForTest("rootless-routing-case", "root", "ses_root_b")
    expect(caseDirectories).toEqual(["rootless-routing-case", processCaseDirectory, rootBCaseDirectory])

    const readRawEvents = async (caseDirectory: string) =>
      fs.readFile(path.join(dir, caseDirectory, "raw-events.jsonl"), "utf8")
    const rootA = await readRawEvents("rootless-routing-case")
    const rootB = await readRawEvents(rootBCaseDirectory)
    const processTrace = await readRawEvents(processCaseDirectory)

    expect(rootA).toContain("owner only")
    expect(rootA).not.toContain("unknown only")
    expect(rootA).not.toContain("rootless only")
    expect(rootB).not.toContain("owner only")
    expect(rootB).not.toContain("unknown only")
    expect(rootB).not.toContain("rootless only")
    expect(processTrace).toContain("rootless only")
    expect(processTrace).toContain("unknown only")
    expect(processTrace).not.toContain("owner only")
  })

  test("keeps long routed case IDs unique, bounded, and stable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-long-route-id-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "long-route-id.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const baseCaseID = `base-${"x".repeat(180)}`
    const sessionA = `ses-${"shared".repeat(24)}-tail_a`
    const sessionB = `ses-${"shared".repeat(24)}-tail_b`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "first", session_id: "ses_first", input: { text: "first" } })`,
        `CaseTrace.promptAssembly({ stage: "long_a", session_id: ${JSON.stringify(sessionA)}, input: { text: "long A" } })`,
        `CaseTrace.promptAssembly({ stage: "long_b", session_id: ${JSON.stringify(sessionB)}, input: { text: "long B" } })`,
        `CaseTrace.finishAll({ status: "success" })`,
      ].join("\n"),
    )

    const run = async (traceDir: string) => {
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: baseCaseID,
          OPENCODE_CASE_TRACE_DIR: traceDir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      expect(await proc.exited).toBe(0)
      expect(await new Response(proc.stderr).text()).toBe("")
      const directories = (await fs.readdir(traceDir, { withFileTypes: true }))
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
      const bySession: Record<string, string> = {}
      for (const directory of directories) {
        const manifest = JSON.parse(await fs.readFile(path.join(traceDir, directory, "manifest.json"), "utf8")) as any
        bySession[manifest.session_id] = directory
      }
      return bySession
    }

    const firstDir = path.join(dir, "first-run")
    const secondDir = path.join(dir, "second-run")
    await fs.mkdir(firstDir)
    await fs.mkdir(secondDir)
    const first = await run(firstDir)
    const second = await run(secondDir)

    expect(first).toEqual(second)
    expect(Object.values(first)).toHaveLength(3)
    expect(new Set(Object.values(first)).size).toBe(3)
    expect(Object.values(first).every((caseID) => caseID.length <= 160)).toBe(true)
    expect(first[sessionA]).toContain("tail_a")
    expect(first[sessionB]).toContain("tail_b")
  })

  test("keeps sanitized root session collisions in distinct case directories", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-session-id-collision-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "session-id-collision.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "first", session_id: "ses_first", input: { text: "first root content" } })`,
        `CaseTrace.promptAssembly({ stage: "slash", session_id: "ses/a", input: { text: "slash root content" } })`,
        `CaseTrace.promptAssembly({ stage: "underscore", session_id: "ses_a", input: { text: "underscore root content" } })`,
        `CaseTrace.finishAll({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "collision-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const traces = await Promise.all(
      (await fs.readdir(dir, { withFileTypes: true }))
        .filter((entry) => entry.isDirectory())
        .map(async (entry) => ({
          directory: entry.name,
          manifest: JSON.parse(await fs.readFile(path.join(dir, entry.name, "manifest.json"), "utf8")) as any,
          trace: await fs.readFile(path.join(dir, entry.name, "trace.json"), "utf8"),
        })),
    )

    expect(traces).toHaveLength(3)
    expect(new Set(traces.map((trace) => trace.directory)).size).toBe(3)
    expect(traces.map((trace) => trace.manifest.session_id).sort()).toEqual(["ses/a", "ses_a", "ses_first"])

    const first = traces.find((trace) => trace.manifest.session_id === "ses_first")!
    const slash = traces.find((trace) => trace.manifest.session_id === "ses/a")!
    const underscore = traces.find((trace) => trace.manifest.session_id === "ses_a")!

    expect(first.directory).toBe("collision-case")
    expect(first.trace).toContain("first root content")
    expect(slash.directory).toMatch(/^collision-case--ses_a--[0-9a-f]{12}$/)
    expect(underscore.directory).toMatch(/^collision-case--ses_a--[0-9a-f]{12}$/)
    expect(slash.directory).not.toBe(underscore.directory)
    expect(slash.trace).toContain("slash root content")
    expect(underscore.trace).toContain("underscore root content")
    expect(slash.trace).not.toContain("underscore root content")
    expect(underscore.trace).not.toContain("slash root content")
  })

  test("reserves an exact 160-character base for the first root when a routed identity collides with it", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-reserved-base-collision-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "reserved-base-collision.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const routedSuffix = `--ses_a--${routedIdentityDigestForTest("root", "ses/a")}`
    const baseCaseID = `${"b".repeat(160 - routedSuffix.length)}${routedSuffix}`

    expect(baseCaseID).toHaveLength(160)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "first", session_id: "ses_first", input: { text: "first root content" } })`,
        `CaseTrace.promptAssembly({ stage: "slash", session_id: "ses/a", input: { text: "slash root content" } })`,
        `CaseTrace.finishAll({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: baseCaseID,
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const traces = await Promise.all(
      (await fs.readdir(dir, { withFileTypes: true }))
        .filter((entry) => entry.isDirectory())
        .map(async (entry) => ({
          directory: entry.name,
          manifest: JSON.parse(await fs.readFile(path.join(dir, entry.name, "manifest.json"), "utf8")) as any,
          trace: await fs.readFile(path.join(dir, entry.name, "trace.json"), "utf8"),
        })),
    )

    const first = traces.find((trace) => trace.manifest.session_id === "ses_first")!
    const slash = traces.find((trace) => trace.manifest.session_id === "ses/a")!
    const disambiguated = `${baseCaseID.slice(0, 160 - `${routedSuffix}--1`.length)}${routedSuffix}--1`

    expect(traces).toHaveLength(2)
    expect(first.directory).toBe(baseCaseID)
    expect(slash.directory).toBe(disambiguated)
    expect(first.trace).toContain("first root content")
    expect(first.trace).not.toContain("slash root content")
    expect(slash.trace).toContain("slash root content")
    expect(slash.trace).not.toContain("first root content")
  })

  test("clears routed case ID allocations when reconfiguring the same base", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-reconfigured-base-collision-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "reconfigured-base-collision.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const routedSuffix = `--ses_a--${routedIdentityDigestForTest("root", "ses/a")}`
    const baseCaseID = `${"b".repeat(160 - routedSuffix.length)}${routedSuffix}`
    const disambiguated = `${baseCaseID.slice(0, 160 - `${routedSuffix}--1`.length)}${routedSuffix}--1`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "first", session_id: "ses_first", input: { text: "first lifecycle root" } })`,
        `CaseTrace.promptAssembly({ stage: "slash", session_id: "ses/a", input: { text: "first lifecycle slash" } })`,
        `CaseTrace.finishAll({ status: "success" })`,
        `const reconfigured = CaseTrace.configure({ caseID: ${JSON.stringify(baseCaseID)} })`,
        `CaseTrace.promptAssembly({ stage: "first", session_id: "ses_first", input: { text: "second lifecycle root" } })`,
        `CaseTrace.promptAssembly({ stage: "slash", session_id: "ses/a", input: { text: "second lifecycle slash" } })`,
        `CaseTrace.finishAll({ status: "success" })`,
        `process.stdout.write(JSON.stringify({ reconfiguredCaseID: reconfigured?.caseID }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: baseCaseID,
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(JSON.parse(await new Response(proc.stdout).text())).toEqual({ reconfiguredCaseID: baseCaseID })

    const directories = (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()

    expect(directories).toEqual([baseCaseID, disambiguated].sort())
    expect(directories).not.toContain(`${baseCaseID.slice(0, 160 - `${routedSuffix}--2`.length)}${routedSuffix}--2`)
    expect(await fs.readFile(path.join(dir, disambiguated, "trace.json"), "utf8")).toContain("second lifecycle slash")
  })

  test("reconfigures in finally and keeps trace finalization failures passive", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-reconfigure-finalizer-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "reconfigure-finalizer.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const previous = CaseTrace.configure({ caseID: "reconfigure-old" }) as any`,
        `previous.finish = () => { throw new Error("forced reconfigure finalization failure") }`,
        `let reconfigureThrew = false`,
        `let fresh: any`,
        `try { fresh = CaseTrace.configure({ caseID: "reconfigure-new" }) } catch { reconfigureThrew = true }`,
        `CaseTrace.promptAssembly({ stage: "new_lifecycle", session_id: "ses_new", input: { text: "new lifecycle prompt" } })`,
        `const finish = fresh.finish.bind(fresh)`,
        `fresh.finish = () => { throw new Error("forced public finalization failure") }`,
        `let finishThrew = false`,
        `try { CaseTrace.finishAll({ status: "success" }) } catch { finishThrew = true }`,
        `fresh.finish = finish`,
        `CaseTrace.finishAll({ status: "success" })`,
        `process.stdout.write(JSON.stringify({ reconfigureThrew, finishThrew, previousCaseID: previous.caseID, freshCaseID: fresh.caseID, same: previous === fresh }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(JSON.parse(await new Response(proc.stdout).text())).toEqual({
      reconfigureThrew: false,
      finishThrew: false,
      previousCaseID: "reconfigure-old",
      freshCaseID: "reconfigure-new",
      same: false,
    })

    const trace = JSON.parse(await fs.readFile(path.join(dir, "reconfigure-new", "trace.json"), "utf8")) as any
    expect(trace.manifest.case_id).toBe("reconfigure-new")
    expect(trace.manifest.session_id).toBe("ses_new")
    expect(trace.records.some((record: any) => record.data?.stage === "new_lifecycle")).toBe(true)
  })

  test("finish is a no-op before any trace exists", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-empty-finish-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "empty-finish.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(script, [`import { CaseTrace } from ${JSON.stringify(traceModule)}`, `CaseTrace.finish()`].join("\n"))

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "empty-finish-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect((await fs.readdir(dir, { withFileTypes: true })).filter((entry) => entry.isDirectory())).toEqual([])
  })

  test("does not create trace directories when routed APIs are disabled", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-routing-disabled-"))
    const traceRoot = path.join(dir, "traces")
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "routing-disabled.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure()`,
        `CaseTrace.get()`,
        `CaseTrace.setSessionID("ses_disabled")`,
        `CaseTrace.startSpan({ component: "trace", operation: "disabled", input: { sessionID: "ses_disabled" } })`,
        `CaseTrace.aliasSession("ses_child", "ses_disabled")`,
        `CaseTrace.event({ component: "trace", event_type: "disabled", data: { sessionID: "ses_disabled" } })`,
        `CaseTrace.usage({ total: 1 }, "span_disabled")`,
        `CaseTrace.contextSnapshot({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.decision({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.promptAssembly({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.contextTransform({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.edge({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.verification({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.change({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.constraint({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.finalEvidence({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.responseOutput({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.designRecord({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.llmTurn({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.agentLifecycle({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.exitGate({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.evidenceFact({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.node({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.causalEdge({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.observation({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.compaction({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.compactionCheck({ session_id: "ses_disabled" } as any)`,
        `CaseTrace.currentEvidenceRefs()`,
        `CaseTrace.currentSourceRefs()`,
        `CaseTrace.finishSession("ses_disabled", { status: "success" })`,
        `CaseTrace.finishAll({ status: "success" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "0",
        OPENCODE_CASE_ID: "disabled-routing-case",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await exists(traceRoot)).toBe(false)
  })

  test("captures config subject revision at case start and keeps it immutable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-subject-revision-config-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subject-revision-config.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ subjectRevision: "git:config-start", environment: { revision: "git:legacy-untrusted" } })`,
        `CaseTrace.configure({ subjectRevision: "git:late-change", environment: { revision: "git:legacy-changed" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subject-revision-config",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_TRACE_SUBJECT_REVISION: "git:env-fallback",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await proc.exited).toBe(0)

    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "subject-revision-config", "manifest.json"), "utf8"),
    ) as any
    expect(manifest.subject_revision).toBe("git:config-start")
    expect(manifest.subject_revision_provenance).toEqual({
      method: "case_trace_config",
      source: "CaseTraceConfig.subjectRevision",
      bound_at: "case_start",
      case_id: manifest.case_id,
      run_id: manifest.run_id,
    })
    expect(manifest.environment.revision).toBe("git:legacy-changed")
  })

  test("binds artifact-owning causal nodes to the immutable case-start revision", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-node-revision-binding-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "node-revision-binding.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ subjectRevision: "git:case-start" })`,
        `CaseTrace.node({ node_id: "decisionnode_revision_bound", kind: "decision", component: "processor", title: "Implement the fix", data: { subject_revision: "git:forged", revision_provenance_status: "invalid", rationale: ${JSON.stringify("I will implement the missing methods. ".repeat(100))} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "node-revision-binding",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "128",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "node-revision-binding", "trace.json"), "utf8"),
    ) as any
    const owner = trace.records.find((record: any) => record.record_id === "decisionnode_revision_bound")

    expect(owner.artifact_refs.length).toBeGreaterThan(0)
    expect(owner.data).toMatchObject({
      case_id: "node-revision-binding",
      subject_revision: "git:case-start",
      revision_provenance_status: "valid",
    })
  })

  test("lazy serve path captures dedicated subject revision environment variable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-subject-revision-env-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subject-revision-env.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "serve.lazy.start" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subject-revision-env",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_TRACE_SUBJECT_REVISION: "git:serve-env",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await proc.exited).toBe(0)

    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "subject-revision-env", "manifest.json"), "utf8"),
    ) as any
    expect(manifest.subject_revision).toBe("git:serve-env")
    expect(manifest.subject_revision_provenance).toEqual({
      method: "environment_variable",
      source: "OPENCODE_TRACE_SUBJECT_REVISION",
      bound_at: "case_start",
      case_id: manifest.case_id,
      run_id: manifest.run_id,
    })
  })

  test("writes trace semantic contract v6.0 bundle with trace.html as the only HTML entry point", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-bundle-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-bundle.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "fix pricing bug" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_test", agent: "build", model: { providerID: "deepseek", id: "unit-test" }, message_count: 1, system_count: 0, tool_count: 2 } })`,
        `const ctx = CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "unit-test", agent: "build", message_count: 1, messages: [{ role: "user", content: "fix pricing bug" }] })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file", summary: "pricing.mjs owns discount calculation", data: { file: "src/pricing.mjs", lines: "1-20" }, source_refs: ctx ? ["context:" + ctx.snapshot_id] : [] })`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "file", summary: "pricing.mjs owns discount calculation", data: { path: "src/pricing.mjs", symbol: "discount" }, source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.compactionCheck({ session_id: "ses_test", message_id: "msg_user", provider_id: "deepseek", model_id: "unit-test", token_estimate: 120, context_limit: 1000, reserved_tokens: 100, overflow: false, selected_algorithm: "head-tail-summary", trigger_reason: "unit_test_no_overflow" })`,
        `CaseTrace.responseOutput({ text: "Discount bug is in pricing.mjs.", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `span?.end({ output: { completed: true, finish_reason: "stop" }, tokenUsage: { inputTokens: 10, outputTokens: 5, cachedInputTokens: 3, totalTokens: 15 } })`,
        `CaseTrace.finish({ status: "success", result: { exit_code: 0 } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-bundle-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "causal-bundle-case")
    for (const file of [
      "manifest.json",
      "trace.json",
      "legacy-trace.json",
      "records.jsonl",
      "raw-events.jsonl",
      "trace.html",
    ]) {
      expect(await exists(path.join(caseDir, file))).toBe(true)
    }
    expect(await exists(path.join(caseDir, "viewer.html"))).toBe(false)
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(caseDir, "provenance-trace.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    const records = await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")
    const journal = await readCausalIRJournal(caseDir)
    const traceHtml = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const provenanceText = JSON.stringify(provenance)
    const canonicalEdge = trace.edges[0] as NonNullable<ProvenanceTraceSummary["edges"]>[number]
    const originalRelation: string = canonicalEdge.original_relation
    const normalizedRelation: string = canonicalEdge.normalized_relation
    const evidenceTier: "confirmed" | "content_matched" | "temporal_advisory" = canonicalEdge.evidence_tier
    const eligibleForAttribution: boolean = canonicalEdge.eligible_for_attribution
    const derivationMethod: string = canonicalEdge.derivation_method
    const allowedRelations = new Set([
      "selected_into_context",
      "prompted",
      "produced",
      "consumed",
      "compressed_from",
      "compressed_to",
      "spawned",
      "continued_from",
      "derived_from",
      "verified_by",
      "modified_by",
      "failed_before",
      "motivated_by_evidence",
      "read_from",
      "returned_by",
      "submitted",
      "assembled",
      "transformed_to",
      "resolved_to",
      "used_as_context",
      "selected_by",
      "called",
      "returned_to",
      "delegated_to",
      "reported_to",
      "supported_response",
      "claimed_by",
      "supports_claim",
      "contextualizes_claim",
      "executed_for_claim",
      "response_to_claim_group",
      "claim_group_precedes",
    ])

    expect(manifest.trace_version).toBe("6.0")
    expect(manifest.case_id).toBe("causal-bundle-case")
    expect(manifest.files.trace).toBe("trace.json")
    expect(manifest.files.legacy_trace).toBe("legacy-trace.json")
    expect(manifest.files.trace_html).toBe("trace.html")
    expect(manifest.files.viewer_alias).toBeUndefined()
    expect(trace.trace_version).toBe("6.0")
    expect(trace.causal_ir_version).toBe("1.0")
    expect(trace.journal).toMatchObject({
      path: "records.jsonl",
      summary_scope: "entries_before_lifecycle_entry",
      poisoned: false,
    })
    expect(typeof trace.journal.entry_count).toBe("number")
    expect(typeof trace.journal.last_sequence).toBe("number")
    expect(trace.journal.last_payload_hash).toMatch(/^[a-f0-9]{64}$/)
    expect(trace.journal.entry_count).toBe(trace.journal.last_sequence)
    expect(trace.journal.last_sequence).toBe(journal.at(-1).sequence - 1)
    expect(trace.journal.last_payload_hash).toBe(journal.at(-2).payload_hash)
    expect(journal.at(-1).data.snapshot).toBeUndefined()
    expect(journal.at(-1).data.trace).toBeUndefined()
    expect(journal.at(-1).data.canonical).toMatchObject({
      trace_version: trace.trace_version,
      causal_ir_version: trace.causal_ir_version,
      manifest: trace.manifest,
    })
    expect((CausalIRModule as any).replayCausalIRTrace(journal)).toEqual(trace)
    expect(trace.nodes.length).toBe(trace.metrics.records)
    expect(trace.edges.length).toBe(trace.metrics.dataflow_edges)
    expect(trace.artifacts.length).toBe(trace.metrics.artifacts)
    expect(trace.nodes.map((node: any) => node.node_id)).toEqual(trace.records.map((record: any) => record.record_id))
    expect(trace.records).toEqual(provenance.records)
    expect(trace.dataflow_edges).toEqual(provenance.dataflow_edges)
    expect(trace.artifacts).toEqual(provenance.artifacts)
    expect(trace.compatibility.provenance_projection).toBe("provenance-trace.json")
    expect(Object.keys(provenance).sort()).toEqual([
      "artifacts",
      "dataflow_edges",
      "manifest",
      "metrics",
      "records",
      "trace_version",
    ])
    expect(provenance.trace_version).toBe("6.0")
    expect(provenance.causal_ir_version).toBeUndefined()
    expect(provenance.nodes).toBeUndefined()
    expect(provenance.edges).toBeUndefined()
    expect(provenance.diagnostics).toBeUndefined()
    expect(provenance.compatibility).toBeUndefined()
    expect(provenance.metrics.records).toBe(provenance.records.length)
    expect(provenance.metrics.dataflow_edges).toBe(provenance.dataflow_edges.length)
    expect(provenance.metrics.artifacts).toBe(provenance.artifacts.length)
    expect(partial.trace_version).toBe("6.0")
    expect(partial.causal_ir_version).toBe("1.0")
    expect(partial.nodes).toEqual(trace.nodes)
    expect(partial.edges).toEqual(trace.edges)
    assertFinalForcedCheckpointMatchesCanonicalTrace(journal, partial, trace)
    assertJournalReplaysCanonicalTrace(journal, trace)
    expect({ originalRelation, normalizedRelation, evidenceTier, eligibleForAttribution, derivationMethod }).toEqual({
      originalRelation: expect.any(String),
      normalizedRelation: expect.any(String),
      evidenceTier: expect.any(String),
      eligibleForAttribution: expect.any(Boolean),
      derivationMethod: expect.any(String),
    })
    expect(legacy.trace_version).toBe("1.3")
    expect(provenance.metrics.token_usage.total).toBe(15)
    expect(provenanceText).not.toContain("[Circular]")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("run.start")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("context.pack")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("llm.call")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("execution.observation")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("response.output")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("response.claim")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("claim.support_assessment")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("context.compaction_check")
    expect(provenance.records.map((record: any) => record.event_type)).not.toContain("runtime.event")
    expect(provenance.dataflow_edges.every((edge: any) => allowedRelations.has(edge.relation))).toBe(true)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "supported_response")).toBe(true)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "supports_claim")).toBe(true)
    expect(provenanceText).not.toContain("diagnostics_hints")
    expect(provenance.records.every((record: any) => record.evidence_refs === undefined)).toBe(true)
    expect(provenanceText).not.toContain("final.claim")
    expect(records).toContain('"record_type":"node"')
    const llm = provenance.records.find((record: any) => record.event_type === "llm.call")
    expect(llm.data.agent).toBe("build")
    expect(llm.data.provider_id).toBe("deepseek")
    expect(llm.data.model_id).toBe("unit-test")
    expect(llm.data.message_count).toBe(1)
    expect(llm.data.tool_count).toBe(2)
    expect(llm.token_usage.total).toBe(15)
    const response = provenance.records.find((record: any) => record.event_type === "response.output")
    expect(response.data.response_role).toBe("final_answer")
    expect(response.data.is_final_for_case).toBe(true)
    expect(traceHtml).toContain("Trace v6.0")
    expect(traceHtml).toContain('id="overview"')
    expect(traceHtml).toContain('id="trace-health"')
    expect(traceHtml).toContain('id="agent-flow"')
    expect(traceHtml).toContain('id="llm-turns"')
    expect(traceHtml).toContain('id="lifecycle"')
    expect(traceHtml).toContain('id="subagents"')
    expect(traceHtml).toContain('id="claim-evidence-matrix"')
    expect(traceHtml).toContain('id="evidence-facts"')
    expect(traceHtml).toContain("Claim Evidence Matrix")
    expect(traceHtml).toContain("Component Dataflow")
    expect(traceHtml).toContain("IO Inspector")
    expect(traceHtml).toContain("Context And Compaction")
    expect(traceHtml).not.toContain("Evidence Inspector")
  })

  test("round-trips node parent, input, and output refs through canonical and compatibility envelopes", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-causal-node-refs-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "node-refs.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.node({ node_id: "node_refs", kind: "decision", component: "task", span_id: "span_child", parent_span_id: "span_parent", input_refs: ["tool_call:call_1"], output_refs: ["tool_result:result_1"], data: { chosen_action: "inspect" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "node-refs-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "node-refs-case", "trace.json"), "utf8")) as any
    const canonical = trace.nodes.find((item: any) => item.node_id === "node_refs")
    const compatibility = trace.records.find((item: any) => item.record_id === "node_refs")
    expect(canonical.scope.parent_span_id).toBe("span_parent")
    expect(canonical.input_refs).toEqual([{ ref_type: "external", ref_id: "call_1", legacy_ref: "tool_call:call_1" }])
    expect(canonical.output_refs).toEqual([
      { ref_type: "external", ref_id: "result_1", legacy_ref: "tool_result:result_1" },
    ])
    expect(compatibility).toMatchObject({
      parent_span_id: "span_parent",
      input_refs: ["tool_call:call_1"],
      output_refs: ["tool_result:result_1"],
    })
  })

  test("resolves real tool call and result endpoints without a canonical self-loop", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-canonical-tool-identities-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "canonical-tool-identities.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "tool", event_type: "tool.call", data: { callID: "shared_call", tool: "read", input: { path: "src/pricing.ts" } } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { callID: "shared_call", tool: "read", output: { content: "pricing owner is billing" } } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "canonical-tool-identities-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "canonical-tool-identities-case", "trace.json"), "utf8"),
    ) as any
    const call = trace.nodes.find((item: any) => item.kind === "tool.call" && item.scope.call_id === "shared_call")
    const result = trace.nodes.find((item: any) => item.kind === "tool.result" && item.scope.call_id === "shared_call")
    const canonicalEdge = trace.edges.find(
      (item: any) =>
        item.from?.legacy_ref === "tool_call:shared_call" && item.to?.legacy_ref === "tool_result:shared_call",
    )
    const compatibilityEdge = trace.dataflow_edges.find((item: any) => item.edge_id === canonicalEdge?.edge_id)

    expect(call.aliases).toContain("tool_call:shared_call")
    expect(result.aliases).toContain("tool_result:shared_call")
    expect(canonicalEdge).toMatchObject({
      from: { ref_type: "node", ref_id: call.node_id, legacy_ref: "tool_call:shared_call" },
      to: { ref_type: "node", ref_id: result.node_id, legacy_ref: "tool_result:shared_call" },
    })
    expect(canonicalEdge.from.ref_id).not.toBe(canonicalEdge.to.ref_id)
    expect(compatibilityEdge).toMatchObject({
      from: { type: "tool_call", id: "shared_call" },
      to: { type: "tool_result", id: "shared_call" },
    })
  })

  test("projects legacy semantic edges from canonical late-alias state without a second mutable graph", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-canonical-legacy-edges-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "canonical-legacy-edges.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const edge = { edge_id: "late_alias_edge", from: { type: "verification", id: "late_verification" }, to: { type: "external", id: "final_result" }, relation: "failure_to_change", evidence_tier: "content_matched", eligible_for_attribution: true, derivation_method: "explicit_test_fixture", evidence_refs: ["verification:late_verification"], confidence: 0.75 } as const`,
        `CaseTrace.edge({ ...edge, label: "stale edge before alias resolution", metadata: { revision: 1 } })`,
        `CaseTrace.node({ node_id: "late_verification_node", kind: "verification", component: "tool", data: { verification_id: "late_verification" } })`,
        `CaseTrace.edge({ ...edge, label: "resolved edge after alias resolution", metadata: { revision: 2, nested: { preserved: true } } })`,
        `CaseTrace.edge({ edge_id: "legacy_edge_after", from: { type: "external", id: "next_source" }, to: { type: "external", id: "next_target" }, relation: "derived_from" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "canonical-legacy-edges-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "canonical-legacy-edges-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const canonicalEdge = trace.edges.find((item: any) => item.edge_id === "late_alias_edge")
    const compatibilityEdge = trace.dataflow_edges.find((item: any) => item.edge_id === "late_alias_edge")
    const legacyEdges = legacy.dataflow_edges.filter((item: any) => item.edge_id === "late_alias_edge")

    expect(canonicalEdge).toMatchObject({
      from: {
        ref_type: "node",
        ref_id: "late_verification_node",
        legacy_ref: "verification:late_verification",
      },
      to: { ref_type: "external", ref_id: "final_result", legacy_ref: "external:final_result" },
      original_relation: "failure_to_change",
      normalized_relation: "motivated_by_evidence",
      evidence_tier: "content_matched",
      eligible_for_attribution: true,
      derivation_method: "explicit_test_fixture",
      evidence_refs: [
        {
          ref_type: "node",
          ref_id: "late_verification_node",
          legacy_ref: "verification:late_verification",
        },
      ],
      confidence: 0.75,
      label: "resolved edge after alias resolution",
      metadata: { revision: 2, nested: { preserved: true } },
    })
    expect(compatibilityEdge).toMatchObject({
      from: { type: "verification", id: "late_verification" },
      to: { type: "external", id: "final_result" },
      relation: "motivated_by_evidence",
      label: "resolved edge after alias resolution",
      metadata: {
        original_relation: "failure_to_change",
        normalized_relation: "motivated_by_evidence",
        evidence_tier: "content_matched",
        eligible_for_attribution: true,
        derivation_method: "explicit_test_fixture",
        revision: 2,
        nested: { preserved: true },
      },
    })
    expect(legacyEdges).toHaveLength(1)
    expect(legacyEdges[0]).toEqual({
      edge_id: "late_alias_edge",
      from: compatibilityEdge.from,
      to: compatibilityEdge.to,
      relation: "failure_to_change",
      evidence_tier: "content_matched",
      eligible_for_attribution: true,
      derivation_method: "explicit_test_fixture",
      evidence_refs: ["verification:late_verification"],
      confidence: 0.75,
      label: "resolved edge after alias resolution",
      metadata: { revision: 2, nested: { preserved: true } },
    })
    expect(legacy.dataflow_edges.map((item: any) => item.edge_id)).toEqual(["late_alias_edge", "legacy_edge_after"])
    expect(Object.keys(legacy.dataflow_edges[0])).toEqual([
      "edge_id",
      "from",
      "to",
      "relation",
      "evidence_tier",
      "eligible_for_attribution",
      "derivation_method",
      "evidence_refs",
      "confidence",
      "label",
      "metadata",
    ])
    expect(Object.keys(legacy.dataflow_edges[1])).toEqual(["edge_id", "from", "to", "relation"])
    expect(partial.edges).toEqual(trace.edges)
    expect(partial.dataflow_edges).toEqual(trace.dataflow_edges)
    expect(JSON.stringify(trace.dataflow_edges)).not.toContain("__case_trace_legacy_semantic_edge_projection")
    expect(html).toContain("resolved edge after alias resolution")
    expect(html).not.toContain("__case_trace_legacy_semantic_edge_projection")
  })

  test("keeps recent fallback refs temporal advisory and outside attribution source refs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-temporal-advisory-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "temporal-advisory.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "pricing owner is billing", data: { subject: "pricing", predicate: "owner", value: "billing" }, source_refs: [] })`,
        `const generic = CaseTrace.node({ node_id: "temporal_generic", kind: "execution.observation", component: "runtime", input_refs: ["recent_evidence_records", "evidence:" + fact.node_id], output_refs: ["recent_change_records"], source_refs: ["recent_evidence_records"], data: { nested: { input_refs: ["recent_evidence_records", "evidence:" + fact.node_id], evidence_refs: ["recent_verification_records"], payload_refs: ["recent_tool_results"], typed_refs: [{ ref_type: "external", ref_id: "recent_change_records", legacy_ref: "recent_change_records" }] } } })`,
        `const edgeTarget = CaseTrace.node({ node_id: "temporal_edge_target", kind: "execution.observation", component: "runtime", data: { marker: "edge_target" } })`,
        `CaseTrace.setSessionID("ses_temporal")`,
        `CaseTrace.edge({ edge_id: "temporal_legacy_edge", from: { type: "external", id: "explicit_source" }, to: { type: "node", id: edgeTarget.node_id }, relation: "derived_from", evidence_refs: ["recent_evidence_records"] })`,
        `CaseTrace.decision({ decision_id: "temporal_decision", component: "processor", decision_type: "tool_selection", intent: "inspect pricing owner", chosen_action: "read", source_refs: ["recent_evidence_records"], metadata: { payload_refs: ["recent_change_records"] } })`,
        `CaseTrace.promptAssembly({ stage: "temporal_prompt", session_id: "ses_temporal", input: { text: "inspect pricing owner", evidence_refs: ["recent_evidence_records"] }, source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.contextTransform({ stage: "temporal_transform", session_id: "ses_temporal", message_id: "msg_temporal", step: 1, input: { text: "inspect pricing owner", source_refs: ["recent_evidence_records"] }, output: { text: "inspect pricing owner", payload_refs: ["recent_tool_results"] }, source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.compactionCheck({ check_id: "temporal_compaction", session_id: "ses_temporal", overflow: false, trigger_reason: "unit_test", source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.compaction({ trigger: "auto", session_id: "ses_temporal", output_summary: "pricing owner remains billing", result: "success", source_refs: ["recent_evidence_records"], after_context_refs: ["recent_change_records"], context_ledger: { retained_fact_refs: ["recent_evidence_records"], dropped_fact_refs: ["recent_verification_records"] }, metadata: { payload_refs: ["recent_tool_results"] } })`,
        `CaseTrace.change({ change_id: "temporal_change", files: ["src/pricing.ts"], intent: "record temporal policy", source_refs: ["recent_evidence_records"], verification_refs: ["recent_verification_records"], metadata: { evidence_refs: ["recent_evidence_records"] } })`,
        `CaseTrace.compactionCheck({ check_id: "temporal_repeat_1", session_id: "ses_temporal", model_id: "model_repeat", selected_algorithm: "none", overflow: false, trigger_reason: "unit_test", source_refs: ["recent_evidence_records"] })`,
        `const secondFact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "shipping owner is logistics", data: { subject: "shipping", predicate: "owner", value: "logistics" }, source_refs: [] })`,
        `CaseTrace.compactionCheck({ check_id: "temporal_repeat_2", session_id: "ses_temporal", model_id: "model_repeat", selected_algorithm: "none", overflow: false, trigger_reason: "unit_test", source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.responseOutput({ segment_id: "temporal_response", text: "Pricing owner is billing.", source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.exitGate({ gate_id: "temporal_gate", has_final_answer: true, needs_compaction: false, auto_continue: false, synthetic_continue: false, continuation_source: "none", decision: "exit", reason: "response complete", source_refs: ["recent_evidence_records"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "temporal-advisory-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "temporal-advisory-case", "trace.json"), "utf8")) as any
    const legacyText = await fs.readFile(path.join(dir, "temporal-advisory-case", "legacy-trace.json"), "utf8")
    const fact = trace.records.find((item: any) => item.event_type === "evidence.semantic_fact")
    const secondFact = trace.records.find(
      (item: any) => item.event_type === "evidence.semantic_fact" && item.data.canonical_subject === "shipping",
    )
    const response = trace.records.find(
      (item: any) => item.event_type === "response.output" && item.data.segment_id === "temporal_response",
    )
    const exitGate = trace.records.find((item: any) => item.event_type === "exit.gate")
    const generic = trace.records.find((item: any) => item.record_id === "temporal_generic")
    const edgeTarget = trace.records.find((item: any) => item.record_id === "temporal_edge_target")
    const repeatedCheck = trace.records.find(
      (item: any) => item.event_type === "context.compaction_check" && item.data.check_count === 2,
    )
    const policyTargets = [
      generic,
      edgeTarget,
      trace.records.find(
        (item: any) => item.event_type === "decision" && item.data.decision_id === "temporal_decision",
      ),
      trace.records.find((item: any) => item.event_type === "prompt.assembly" && item.data.stage === "temporal_prompt"),
      trace.records.find(
        (item: any) => item.event_type === "context.transform" && item.data.stage === "temporal_transform",
      ),
      trace.records.find(
        (item: any) => item.event_type === "context.compaction_check" && item.data.check_id === "temporal_compaction",
      ),
      trace.records.find((item: any) => item.event_type === "context.compaction" && item.data.trigger === "auto"),
      trace.records.find((item: any) => item.event_type === "change" && item.data.change_id === "temporal_change"),
      repeatedCheck,
      response,
      exitGate,
    ]
    const advisory = temporalAdvisoryEdgesTo(trace, response.record_id).find((item: any) =>
      contextSetMemberRefs(trace, item).includes(`evidence:${fact.record_id}`),
    )
    const compatibilityAdvisory = trace.dataflow_edges.find((item: any) => item.edge_id === advisory?.edge_id)
    const exitAdvisory = temporalAdvisoryEdgesTo(trace, exitGate.record_id).find((item: any) =>
      contextSetMemberRefs(trace, item).includes(`evidence:${fact.record_id}`),
    )

    expect(response.source_refs ?? []).not.toContain(`evidence:${fact.record_id}`)
    expect(response.data.candidate_source_refs ?? []).not.toContain(`evidence:${fact.record_id}`)
    expect(advisory).toMatchObject({
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
      derivation_method: "recent_source_fallback",
    })
    expect(compatibilityAdvisory.metadata).toMatchObject({
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
      derivation_method: "recent_source_fallback",
    })
    expect(exitGate.source_refs ?? []).not.toContain(`evidence:${fact.record_id}`)
    expect(exitAdvisory).toMatchObject({
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
      derivation_method: "recent_source_fallback",
    })
    expect(secondFact).toBeTruthy()
    expect(policyTargets.every(Boolean)).toBe(true)
    for (const target of policyTargets) {
      expect(target.source_refs ?? []).not.toContain("recent_evidence_records")
      expect(target.source_refs ?? []).not.toContain(`evidence:${fact.record_id}`)
      const fallbackEdges = temporalAdvisoryEdgesTo(trace, target.record_id).filter((item: any) =>
        contextSetMemberRefs(trace, item).includes(`evidence:${fact.record_id}`),
      )
      expect(fallbackEdges).not.toHaveLength(0)
      expect(
        fallbackEdges.every(
          (item: any) =>
            item.evidence_tier === "temporal_advisory" &&
            item.eligible_for_attribution === false &&
            item.derivation_method === "recent_source_fallback",
        ),
      ).toBe(true)
    }
    expect(
      temporalAdvisoryEdgesTo(trace, repeatedCheck.record_id).some(
        (item: any) =>
          contextSetMemberRefs(trace, item).includes(`evidence:${secondFact.record_id}`) &&
          item.evidence_tier === "temporal_advisory" &&
          item.eligible_for_attribution === false,
      ),
    ).toBe(true)
    const legacySelectorEdge = trace.edges.find((item: any) => item.edge_id === "temporal_legacy_edge")
    expect(legacySelectorEdge.evidence_refs).toEqual([])
    const compaction = policyTargets.find((item: any) => item.event_type === "context.compaction")
    expect(JSON.stringify(compaction.data.before_context_refs)).not.toContain("recent_")
    for (const node of trace.nodes) {
      for (const field of ["input_refs", "output_refs", "source_refs"])
        expect((node[field] ?? []).some((ref: any) => ref.legacy_ref?.startsWith("recent_"))).toBe(false)
    }
    for (const selector of [
      "recent_evidence_records",
      "recent_verification_records",
      "recent_change_records",
      "recent_tool_results",
    ]) {
      expect(JSON.stringify(trace.nodes)).not.toContain(selector)
      expect(JSON.stringify(trace.records)).not.toContain(selector)
      expect(JSON.stringify(trace.dataflow_edges)).not.toContain(selector)
      expect(legacyText).not.toContain(selector)
    }
  })

  test("marks response claims and support assessments as reproducible deterministic derivations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-derived-response-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "derived-response-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "pricing owner is billing", data: { subject: "pricing", predicate: "owner", value: "billing" }, source_refs: [] })`,
        `CaseTrace.responseOutput({ segment_id: "derived_response", text: "Pricing owner is billing.", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "derived-response-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "derived-response-facts-case", "trace.json"), "utf8"),
    ) as any
    const derived = trace.nodes.filter(
      (item: any) => item.kind === "response.claim" || item.kind === "claim.support_assessment",
    )

    expect(derived.map((item: any) => item.kind)).toEqual(
      expect.arrayContaining(["response.claim", "claim.support_assessment"]),
    )
    for (const node of derived) {
      expect(node.origin).toBe("deterministic_derived")
      expect(node.input_refs.length).toBeGreaterThan(0)
      expect(
        node.input_refs.every((ref: any) => typeof ref.ref_type === "string" && typeof ref.ref_id === "string"),
      ).toBe(true)
      expect(node.derivation).toMatchObject({
        algorithm: expect.any(String),
        algorithm_version: expect.any(String),
        derived_at: expect.any(String),
        reproducible: true,
      })
      expect(node.derivation.input_refs).toEqual(node.input_refs)
    }
  })

  test("links response generation provenance back to LLM, message transform, and context nodes", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-generation-chain-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "generation-chain.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_gen", agent: "build", model: { providerID: "deepseek", id: "unit-test" }, message_count: 2, system_count: 1, tool_count: 1 } })`,
        `const prompt = CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_gen", input: { parts: [{ type: "text", text: "Explain the pricing risk." }] }, output: { part_count: 1 } })`,
        `const transform = CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_gen", message_id: "msg_gen", step: 1, agent: "build", provider_id: "deepseek", model_id: "unit-test", input: { session_messages: [{ role: "user", id: "msg_user" }] }, output: { model_messages: [{ role: "user", content: "Explain the pricing risk." }], tools: { grep: { description: "search" } } }, transforms: [{ name: "MessageV2.toModelMessagesEffect" }], source_refs: prompt ? ["node:" + prompt.node_id] : [] })`,
        `CaseTrace.llmTurn({ span_id: span?.id, session_id: "ses_gen", message_id: "msg_gen", agent: "build", provider_id: "deepseek", model_id: "unit-test", input_context_refs: transform ? ["node:" + transform.node_id] : [], prompt_transform_refs: transform ? ["node:" + transform.node_id] : [], status: "success", finish_reason: "stop", token_usage: { inputTokens: 10, outputTokens: 8, totalTokens: 18 }, source_refs: transform ? ["node:" + transform.node_id] : [] })`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "repo_fact", summary: "pricing risk belongs to billing-platform", data: { subject: "pricing risk", predicate: "owner", value: "billing-platform" } })`,
        `CaseTrace.responseOutput({ text: "Pricing risk belongs to billing-platform.", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `span?.end({ output: { completed: true, finish_reason: "stop" }, tokenUsage: { inputTokens: 10, outputTokens: 8, totalTokens: 18 } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "generation-chain-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "generation-chain-case", "trace.json"), "utf8")) as any
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const llmCall = trace.records.find((record: any) => record.event_type === "llm.call")
    const transform = trace.records.find((record: any) => record.event_type === "context.transform")

    expect(response.data.generation_provenance_refs).toContain(`node:${llmCall.record_id}`)
    expect(response.data.generation_provenance_refs).toContain(`node:${transform.record_id}`)
    expect(claim.data.generation_provenance_refs).toContain(`node:${llmCall.record_id}`)
    expect(claim.data.generation_provenance_refs).toContain(`node:${transform.record_id}`)
    expect(llmCall.data.message_transforms[0].node_ref).toBe(`node:${transform.record_id}`)
    expect(llmCall.data.input_messages).toBeTruthy()
    expect(llmCall.data.selected_context_refs).toContain(`node:${transform.record_id}`)
    expect(llmCall.data.output_text).toBeTruthy()
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.type === "node" &&
          edge.from.id === llmCall.record_id &&
          edge.to.id === response.record_id &&
          edge.relation === "produced",
      ),
    ).toBe(true)
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.type === "node" &&
          edge.from.id === transform.record_id &&
          edge.to.id === response.record_id &&
          edge.relation === "used_as_context",
      ),
    ).toBe(true)
  })

  test("links tool result through request context and LLM generation to a reasoning decision", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-decision-generation-chain-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "decision-generation-chain.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "inspect then decide" }, environment: { model: "unit-test" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "bash", callID: "call_tests", sessionID: "ses_decision", messageID: "msg_tool", output: "18 assertions passed" } })`,
        `const transform = CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_decision", message_id: "msg_assistant", step: 2, agent: "build", provider_id: "deepseek", model_id: "unit-test", input: { session_messages: [{ role: "tool", toolCallId: "call_tests", content: "18 assertions passed" }] }, output: { model_messages: [{ role: "tool", toolCallId: "call_tests", content: "18 assertions passed" }] }, transforms: [{ name: "MessageV2.toModelMessagesEffect" }] })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_decision", messageID: "msg_assistant", agent: "build", model: { providerID: "deepseek", id: "unit-test" } } })`,
        `CaseTrace.llmTurn({ span_id: span?.id, session_id: "ses_decision", message_id: "msg_assistant", agent: "build", provider_id: "deepseek", model_id: "unit-test", input_context_refs: transform ? ["node:" + transform.node_id] : [], source_refs: transform ? ["node:" + transform.node_id] : [], status: "success", finish_reason: "tool-calls" })`,
        `CaseTrace.decision({ component: "processor", decision_type: "reasoning_block", intent: "interpret verification", chosen_action: "report success", rationale: "The observed assertions passed.", metadata: { sessionID: "ses_decision", messageID: "msg_assistant" } })`,
        `span?.end({ output: { completed: true, finish_reason: "tool-calls" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "decision-generation-chain-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "decision-generation-chain-case", "trace.json"), "utf8"),
    ) as any
    const toolResult = trace.records.find((record: any) => record.event_type === "tool.result")
    const transform = trace.records.find((record: any) => record.event_type === "context.transform")
    const llm = trace.records.find((record: any) => record.event_type === "llm.call")
    const decision = trace.records.find(
      (record: any) => record.event_type === "decision" && record.data.decision_type === "reasoning_block",
    )
    const contextSetRef = transform.data.inferred_tool_context_set_ref
    const contextSet = trace.records.find((record: any) => `node:${record.record_id}` === contextSetRef)

    expect(transform.source_refs).toContain(contextSetRef)
    expect(transform.source_refs).not.toContain(`node:${toolResult.record_id}`)
    expect(contextSet.data.member_refs).toContain(`node:${toolResult.record_id}`)
    expect(decision.source_refs).toEqual(
      expect.arrayContaining([`node:${transform.record_id}`, `node:${llm.record_id}`]),
    )
    expect(
      trace.edges.some(
        (edge: any) =>
          edge.from.ref_id === toolResult.record_id &&
          edge.to.ref_id === contextSet.record_id &&
          edge.normalized_relation === "selected_into_context",
      ),
    ).toBe(true)
    expect(
      trace.edges.some(
        (edge: any) =>
          edge.from.ref_id === contextSet.record_id &&
          edge.to.ref_id === transform.record_id &&
          edge.normalized_relation === "used_as_context",
      ),
    ).toBe(true)
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.id === llm.record_id && edge.to.id === decision.record_id && edge.relation === "produced",
      ),
    ).toBe(true)
  })

  test("keeps decision generation provenance within the decision session", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-decision-session-scope-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "decision-session-scope.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "parent request" }, environment: { model: "unit-test" } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_parent", message_id: "msg_parent", input: { messages: ["parent"] }, output: { model_messages: ["parent"] } })`,
        `CaseTrace.llmTurn({ session_id: "ses_parent", message_id: "msg_parent", agent: "build", provider_id: "deepseek", model_id: "unit-test", status: "success", finish_reason: "tool-calls" })`,
        `CaseTrace.aliasSession("ses_child", "ses_parent")`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_child", message_id: "msg_child", input: { messages: ["child"] }, output: { model_messages: ["child"] } })`,
        `CaseTrace.llmTurn({ session_id: "ses_child", message_id: "msg_child", agent: "general", provider_id: "deepseek", model_id: "unit-test", status: "success", finish_reason: "tool-calls" })`,
        `CaseTrace.decision({ component: "processor", decision_type: "reasoning_block", chosen_action: "edit parent", rationale: "parent reasoning", metadata: { sessionID: "ses_parent", messageID: "msg_parent" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "decision-session-scope-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "decision-session-scope-case", "trace.json"), "utf8"),
    ) as any
    const decision = trace.records.find(
      (record: any) => record.event_type === "decision" && record.data.chosen_action === "edit parent",
    )
    const referenced = decision.source_refs.map((ref: string) => ref.replace(/^node:/, ""))
    const referencedNodes = trace.records.filter((record: any) => referenced.includes(record.record_id))

    expect(referencedNodes.some((record: any) => record.data.session_id === "ses_parent")).toBe(true)
    expect(referencedNodes.some((record: any) => record.data.session_id === "ses_child")).toBe(false)
  })

  test("scopes production-shaped generation nodes and context snapshots to one assistant turn", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-production-generation-scope-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "production-generation-scope.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "parent request" }, environment: { model: "unit-test" } })`,
        `CaseTrace.contextTransform({ stage: "model_messages_built", session_id: "ses_parent", message_id: "msg_parent_assistant", input: { messages: ["parent"] }, output: { model_messages: ["parent"] } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_parent", input: { messages: ["parent-ready"] }, output: { model_messages: ["parent-ready"] } })`,
        `const parentSpan = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_parent", agent: "build", model: { providerID: "deepseek", id: "unit-test" } } })`,
        `const parentSnapshot = CaseTrace.contextSnapshot({ span_id: parentSpan?.id, phase: "llm_request", agent: "build", messages: ["parent"], metadata: { session_id: "ses_parent", message_id: "msg_parent_assistant" } })`,
        `CaseTrace.aliasSession("ses_child", "ses_parent")`,
        `CaseTrace.contextTransform({ stage: "model_messages_built", session_id: "ses_child", message_id: "msg_child_assistant", input: { messages: ["child"] }, output: { model_messages: ["child"] } })`,
        `const childSpan = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_child", agent: "general", model: { providerID: "deepseek", id: "unit-test" } } })`,
        `const childSnapshot = CaseTrace.contextSnapshot({ span_id: childSpan?.id, phase: "llm_request", agent: "general", messages: ["child"], metadata: { session_id: "ses_child", message_id: "msg_child_assistant" } })`,
        `CaseTrace.decision({ component: "processor", decision_type: "reasoning_block", chosen_action: "edit parent", rationale: "parent reasoning", metadata: { sessionID: "ses_parent", messageID: "msg_parent_assistant" } })`,
        `parentSpan?.end({ output: { completed: true } })`,
        `childSpan?.end({ output: { completed: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "production-generation-scope-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "production-generation-scope-case", "trace.json"), "utf8"),
    ) as any
    const decision = trace.records.find(
      (record: any) => record.event_type === "decision" && record.data.chosen_action === "edit parent",
    )
    const selected = decision.data.selected_context_refs as string[]
    const generatedBy = decision.source_refs
      .map((ref: string) => ref.replace(/^node:/, ""))
      .map((id: string) => trace.records.find((record: any) => record.record_id === id))
      .filter(Boolean)
    const parentSnapshot = trace.records.find(
      (record: any) => record.event_type === "context.pack" && record.data.metadata?.session_id === "ses_parent",
    )
    const childSnapshot = trace.records.find(
      (record: any) => record.event_type === "context.pack" && record.data.metadata?.session_id === "ses_child",
    )

    expect(
      generatedBy.some((record: any) => record.event_type === "llm.call" && record.data.session_id === "ses_parent"),
    ).toBe(true)
    expect(generatedBy.some((record: any) => record.data.session_id === "ses_child")).toBe(false)
    expect(selected).toContain(`context_snapshot:${parentSnapshot.data.snapshot_id}`)
    expect(selected).not.toContain(`context_snapshot:${childSnapshot.data.snapshot_id}`)
  })

  test("selects tool outcomes only by structured call identity in the same session", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-exact-tool-context-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "exact-tool-context.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "inspect" }, environment: { model: "unit-test" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.call", data: { sessionID: "ses_owner", messageID: "msg_tool", callID: "call_1", tool: "read", input: { path: "owner.txt" } } })`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "owner observation", data: { session_id: "ses_owner", call_id: "call_1", output: "owner pending" }, source_refs: ["tool_call:call_1"] })`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "unscoped legacy observation", data: { call_id: "call_1", output: "ambiguous pending" }, source_refs: ["tool_call:call_1"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { sessionID: "ses_owner", messageID: "msg_tool", callID: "call_1", tool: "read", output: "owner result" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.call", data: { sessionID: "ses_other", messageID: "msg_other_tool", callID: "call_1", tool: "read", input: { path: "other.txt" } } })`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "other observation", data: { session_id: "ses_other", call_id: "call_1", output: "other pending" }, source_refs: ["tool_call:call_1"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { sessionID: "ses_other", messageID: "msg_other_tool", callID: "call_1", tool: "read", output: "other result" } })`,
        `CaseTrace.contextTransform({ stage: "ordinary_text", session_id: "ses_owner", message_id: "msg_owner", input: { text: "Do not confuse call_10 with another identifier." }, output: { messages: [{ role: "user", content: "call_10" }] } })`,
        `CaseTrace.contextTransform({ stage: "other_session", session_id: "ses_other", message_id: "msg_other", input: { messages: [{ role: "tool", toolCallId: "call_1", content: "foreign" }] }, output: { model_messages: [{ role: "tool", toolCallId: "call_1", content: "foreign" }] } })`,
        `CaseTrace.contextTransform({ stage: "owner_session", session_id: "ses_owner", message_id: "msg_owner", input: { messages: [{ role: "tool", toolCallId: "call_1", content: "owner result" }] }, output: { model_messages: [{ role: "tool", toolCallId: "call_1", content: "owner result" }] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "exact-tool-context-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "exact-tool-context-case", "trace.json"), "utf8")) as any
    const otherTrace = JSON.parse(
      await fs.readFile(
        path.join(dir, routedCaseDirectoryForTest("exact-tool-context-case", "root", "ses_other"), "trace.json"),
        "utf8",
      ),
    ) as any
    const transforms = Object.fromEntries(
      [...trace.records, ...otherTrace.records]
        .filter((record: any) => record.event_type === "context.transform")
        .map((record: any) => [record.data.stage, record]),
    )
    const ownerResult = trace.records.find(
      (record: any) => record.event_type === "tool.result" && record.data.session_id === "ses_owner",
    )
    const otherResult = otherTrace.records.find(
      (record: any) => record.event_type === "tool.result" && record.data.session_id === "ses_other",
    )
    const ownerCall = trace.records.find(
      (record: any) => record.event_type === "tool.call" && record.data.session_id === "ses_owner",
    )
    const otherCall = otherTrace.records.find(
      (record: any) => record.event_type === "tool.call" && record.data.session_id === "ses_other",
    )
    const ownerObservation = trace.records.find(
      (record: any) => record.event_type === "execution.observation" && record.data.data?.session_id === "ses_owner",
    )
    const otherObservation = otherTrace.records.find(
      (record: any) => record.event_type === "execution.observation" && record.data.data?.session_id === "ses_other",
    )
    const unscopedObservation = trace.records.find(
      (record: any) =>
        record.event_type === "execution.observation" && record.data.summary === "unscoped legacy observation",
    )

    expect(transforms.ordinary_text.data.inferred_tool_context_refs).toEqual([])
    expect(ownerResult.record_id).not.toBe(otherResult.record_id)
    expect(transforms.other_session.data.inferred_tool_context_refs).toEqual([`node:${otherResult.record_id}`])
    expect(transforms.owner_session.data.inferred_tool_context_refs).toEqual([`node:${ownerResult.record_id}`])
    expect(ownerObservation.source_refs).toContain(`node:${ownerResult.record_id}`)
    expect(ownerObservation.source_refs).not.toContain(`node:${otherResult.record_id}`)
    expect(otherObservation.source_refs).toContain(`node:${otherResult.record_id}`)
    expect(otherObservation.source_refs).not.toContain(`node:${ownerResult.record_id}`)
    expect(unscopedObservation.source_refs).not.toContain(`node:${ownerResult.record_id}`)
    expect(unscopedObservation.source_refs).not.toContain(`node:${otherResult.record_id}`)
    expect(
      trace.edges.some(
        (edge: any) => edge.from.ref_id === ownerCall.record_id && edge.to.ref_id === ownerResult.record_id,
      ),
    ).toBe(true)
    expect(
      otherTrace.edges.some(
        (edge: any) => edge.from.ref_id === otherCall.record_id && edge.to.ref_id === otherResult.record_id,
      ),
    ).toBe(true)
  })

  test("reuses one confirmed context set across transforms of the same model request", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-confirmed-context-set-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "confirmed-context-set.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "tool", event_type: "tool.call", data: { sessionID: "ses_set", messageID: "msg_tool", callID: "call_set", tool: "read", input: { path: "owner.txt" } } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { sessionID: "ses_set", messageID: "msg_tool", callID: "call_set", tool: "read", output: "owner is billing" } })`,
        `const modelMessages = [{ role: "tool", toolCallId: "call_set", content: "owner is billing" }]`,
        `CaseTrace.contextTransform({ stage: "model_messages_built", session_id: "ses_set", message_id: "msg_request", input: { messages: modelMessages }, output: { model_messages: modelMessages } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_set", message_id: "msg_request", input: { model_messages: modelMessages }, output: { model_messages: modelMessages } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "confirmed-context-set-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "confirmed-context-set-case", "trace.json"), "utf8"),
    ) as any
    const toolResult = trace.records.find((record: any) => record.event_type === "tool.result")
    const transforms = trace.records.filter((record: any) => record.event_type === "context.transform")
    const sets = trace.records.filter(
      (record: any) =>
        record.event_type === "context.pack" && record.data.context_set_kind === "confirmed_tool_selection",
    )
    expect(sets).toHaveLength(1)
    expect(sets[0].data.member_refs).toEqual([`node:${toolResult.record_id}`])
    expect(transforms).toHaveLength(2)
    expect(transforms.every((record: any) => record.source_refs?.includes(`node:${sets[0].record_id}`))).toBe(true)
    expect(transforms.every((record: any) => !record.source_refs?.includes(`node:${toolResult.record_id}`))).toBe(true)
    expect(
      trace.edges.filter(
        (edge: any) =>
          edge.from.ref_id === toolResult.record_id &&
          edge.to.ref_id === sets[0].record_id &&
          edge.normalized_relation === "selected_into_context",
      ),
    ).toHaveLength(1)
    expect(
      trace.edges.filter(
        (edge: any) =>
          edge.from.ref_id === sets[0].record_id &&
          transforms.some((record: any) => record.record_id === edge.to.ref_id) &&
          edge.normalized_relation === "used_as_context",
      ),
    ).toHaveLength(2)
    expect(
      trace.edges.some(
        (edge: any) =>
          edge.from.ref_id === toolResult.record_id &&
          transforms.some((record: any) => record.record_id === edge.to.ref_id),
      ),
    ).toBe(false)
  })

  test("reuses payload-only temporal advisory sets without member-to-target edges", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-temporal-context-set-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "temporal-context-set.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const first = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "owner is billing", data: { subject: "owner", value: "billing" }, source_refs: [] })`,
        `const second = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "cap is 15 percent", data: { subject: "cap", value: "15%" }, source_refs: [] })`,
        `CaseTrace.node({ node_id: "temporal_set_target_1", kind: "execution.observation", component: "runtime", source_refs: ["recent_evidence_records"], data: { target: 1 } })`,
        `CaseTrace.node({ node_id: "temporal_set_target_2", kind: "execution.observation", component: "runtime", source_refs: ["recent_evidence_records"], data: { target: 2 } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "temporal-context-set-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "temporal-context-set-case", "trace.json"), "utf8"),
    ) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const targets = trace.records.filter((record: any) => record.record_id.startsWith("temporal_set_target_"))
    const allSets = trace.records.filter(
      (record: any) => record.event_type === "context.pack" && record.data.context_set_kind === "temporal_advisory",
    )
    const targetIDs = new Set(targets.map((record: any) => record.record_id))
    const sets = allSets.filter((record: any) =>
      trace.edges.some((edge: any) => edge.from.ref_id === record.record_id && targetIDs.has(edge.to.ref_id)),
    )
    expect(sets).toHaveLength(1)
    expect(sets[0].data.member_refs.sort()).toEqual(facts.map((record: any) => `evidence:${record.record_id}`).sort())
    expect(sets[0].source_refs ?? []).toEqual([])
    expect(
      trace.edges.filter(
        (edge: any) =>
          edge.from.ref_id === sets[0].record_id &&
          targets.some((record: any) => record.record_id === edge.to.ref_id) &&
          edge.derivation_method === "recent_source_fallback",
      ),
    ).toHaveLength(2)
    expect(
      trace.edges.some(
        (edge: any) =>
          facts.some((record: any) => record.record_id === edge.from.ref_id) &&
          targets.some((record: any) => record.record_id === edge.to.ref_id),
      ),
    ).toBe(false)
  })

  test("grounds final claims from confirmed generation context without attributing rejected candidates", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-final-claim-grounding-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "final-claim-grounding.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "report the active discount cap" }, environment: { model: "unit-test" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_current", sessionID: "ses_grounding", messageID: "msg_tool_current", output: "renewalQuote has an active 15 percent total discount cap" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "current discount requirement", data: { subject: "renewalQuote", predicate: "discount_cap", value: "15 percent" }, source_refs: ["tool_result:call_current"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_old", sessionID: "ses_grounding", messageID: "msg_tool_old", output: "the stale design allowed a 20 percent discount cap" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "old discount design", data: { subject: "discount", predicate: "discount_cap", value: "20 percent" }, source_refs: ["tool_result:call_old"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_code", sessionID: "ses_grounding", messageID: "msg_tool_code", output: "const discount = Math.min(total, 0.15); return Math.round(base * (1 - discount))" } })`,
        `CaseTrace.change({ change_id: "discount_fix", files: ["src/pricing.mjs"], diff: "- Math.min(total, 0.2)\\n+ Math.min(total, 0.15)", source_refs: ["tool_result:call_code"] })`,
        `CaseTrace.verification({ verification_id: "post_change", command: "npm test", exit_code: 0, status: "passed", stdout: "all tests passed", source_refs: ["change:discount_fix"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_unrelated", sessionID: "ses_grounding", messageID: "msg_tool_unrelated", output: "shipping owner is logistics" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "shipping owner", data: { subject: "shipping", predicate: "owner", value: "logistics" }, source_refs: ["tool_result:call_unrelated"] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_outside", sessionID: "ses_grounding", messageID: "msg_tool_outside", output: "tax owner is finance" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "tax owner", data: { subject: "tax", predicate: "owner", value: "finance" }, source_refs: ["tool_result:call_outside"] })`,
        `const transform = CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_grounding", message_id: "msg_grounding", agent: "build", provider_id: "deepseek", model_id: "unit-test", input: { messages: [{ role: "tool", toolCallId: "call_current", content: "15 percent" }, { role: "tool", toolCallId: "call_old", content: "20 percent" }, { role: "tool", toolCallId: "call_code", content: "Math.round" }, { role: "tool", toolCallId: "call_unrelated", content: "shipping owner" }] }, output: { model_messages: [{ role: "tool", toolCallId: "call_current", content: "15 percent" }, { role: "tool", toolCallId: "call_old", content: "20 percent" }, { role: "tool", toolCallId: "call_code", content: "Math.round" }, { role: "tool", toolCallId: "call_unrelated", content: "shipping owner" }] } })`,
        `CaseTrace.llmTurn({ session_id: "ses_grounding", message_id: "msg_grounding", agent: "build", provider_id: "deepseek", model_id: "unit-test", input_context_refs: transform ? ["node:" + transform.node_id] : [], source_refs: transform ? ["node:" + transform.node_id] : [], status: "success", finish_reason: "stop" })`,
        `CaseTrace.responseOutput({ text: "The old discount cap was changed from 20% to the active renewalQuote cap of 15%, and npm test passed. Math.round handles the final rounding.", metadata: { sessionID: "ses_grounding", messageID: "msg_grounding", response_role: "final_answer", visibility: "user_visible", is_final_for_case: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "final-claim-grounding-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "12000",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "final-claim-grounding-case", "trace.json"), "utf8"),
    ) as any
    const current = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.canonical_subject === "renewalQuote",
    )
    const unrelated = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.canonical_subject === "shipping",
    )
    const old = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.canonical_subject === "discount",
    )
    const outside = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.canonical_subject === "tax",
    )
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const claim = trace.records.find(
      (record: any) => record.event_type === "response.claim" && String(record.data.text).includes("discount cap"),
    )
    const codeClaim = trace.records.find(
      (record: any) => record.event_type === "response.claim" && String(record.data.text).includes("Math.round"),
    )
    const assessment = trace.records.find(
      (record: any) => record.event_type === "claim.support_assessment" && record.data.claim_id === claim.data.claim_id,
    )
    const currentRef = `evidence:${current.record_id}`
    const oldRef = `evidence:${old.record_id}`
    const unrelatedRef = `evidence:${unrelated.record_id}`
    const outsideRef = `evidence:${outside.record_id}`

    expect(response.data.generation_grounding_candidate_refs).toEqual(
      expect.arrayContaining([currentRef, oldRef, unrelatedRef]),
    )
    expect(response.data.generation_grounding_candidate_refs).not.toContain(outsideRef)
    expect(
      response.data.generation_grounding_candidate_refs.some((ref: string) => ref.startsWith("node:toolresult")),
    ).toBe(false)
    expect(claim.data.direct_evidence_refs).toContain(currentRef)
    expect(claim.data.direct_evidence_refs).toContain(oldRef)
    expect(claim.data.direct_evidence_refs).not.toContain(unrelatedRef)
    expect(claim.data.direct_support_refs).toContain("change:discount_fix")
    expect(claim.data.direct_support_refs).toContain("verification:post_change")
    expect(claim.data.grounding_candidate_refs).toEqual(expect.arrayContaining([currentRef, oldRef, unrelatedRef]))
    expect(claim.data.grounding_method).toBe("confirmed_context_semantic_match_v1")
    expect(claim.data.grounding_behavior_impact).toBe("none")
    expect(claim.data.quality_flags).not.toContain("weak_evidence_match")
    expect(claim.data.grounding_decisions).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          candidate_ref: currentRef,
          decision: "selected_direct_support",
          candidate_origin: "confirmed_generation_context",
          agent_attention_observed: false,
          behavior_impact: "none",
        }),
        expect.objectContaining({
          candidate_ref: unrelatedRef,
          decision: "rejected_no_match",
          rejection_reason: "semantic_match_below_threshold",
          agent_attention_observed: false,
          behavior_impact: "none",
        }),
      ]),
    )
    expect(assessment.data.grounding_decisions).toEqual(claim.data.grounding_decisions)
    expect(codeClaim.data.direct_evidence_refs).toContain("tool_result:call_code")
    expect(codeClaim.data.quality_flags).not.toContain("unsupported_response_claim")
    expect(
      trace.edges.some((edge: any) => edge.from.ref_id === current.record_id && edge.to.ref_id === claim.record_id),
    ).toBe(true)
    expect(
      trace.edges.some((edge: any) => edge.from.ref_id === unrelated.record_id && edge.to.ref_id === claim.record_id),
    ).toBe(false)
  })

  test("writes v6.0 semantic pipeline records for prompt assembly, context transforms, and decisions", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v45-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-v45.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "find owner of pricing API" }, environment: { model: "unit-test" } })`,
        `const prompt = CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_v45", message_id: "msg_user", agent: "build", input: { parts: [{ type: "text", text: "find owner of pricing API" }] }, output: { part_count: 1 } })`,
        `const transform = CaseTrace.contextTransform({ stage: "model_messages_built", session_id: "ses_v45", message_id: "msg_assistant", step: 1, agent: "build", provider_id: "deepseek", model_id: "unit-test", input: { session_messages: [{ role: "user", id: "msg_user" }] }, output: { model_messages: [{ role: "user", content: "find owner of pricing API" }], system: ["system prompt"], tools: { grep: { description: "search" } } }, transforms: [{ name: "MessageV2.toModelMessagesEffect" }], source_refs: prompt ? ["prompt:" + prompt.node_id] : [] })`,
        `const decision = CaseTrace.decision({ component: "processor", decision_type: "llm_tool_call", intent: "search pricing owner", chosen_action: "grep", rationale: { recent_reasoning: "Need source evidence before answering", input: { pattern: "pricing" } }, source_refs: transform ? ["context:" + transform.node_id] : [] })`,
        `CaseTrace.edge({ from: { type: "decision", id: decision?.decision_id ?? "missing" }, to: { type: "tool_call", id: "call_grep", label: "grep" }, relation: "decision_to_tool", label: "Model selected grep" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-v45-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "semantic-v45-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const eventTypes = trace.records.map((record: any) => record.event_type)

    expect(trace.trace_version).toBe("6.0")
    expect(eventTypes).toContain("prompt.assembly")
    expect(eventTypes).toContain("context.transform")
    expect(eventTypes).toContain("decision")
    expect(trace.dataflow_edges.some((edge: any) => edge.relation === "selected_by")).toBe(true)
    expect(html).toContain("Semantic Pipeline")
    expect(html).toContain("prompt.assembly")
    expect(html).toContain("context.transform")
    expect(html).toContain("llm_tool_call")
  })

  test("records unresolved user-requested skills as formal skill.load facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v48-skill-request-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "skill-request-v48.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const prompt = CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_skill", message_id: "msg_user", agent: "build", input: { parts: [{ type: "text", text: "请使用 repo-audit skill 审计仓库" }] }, output: { part_count: 1 } })`,
        `CaseTrace.contextTransform({ stage: "model_messages_built", session_id: "ses_skill", message_id: "msg_assistant", step: 1, agent: "build", provider_id: "deepseek", model_id: "unit-test", input: { session_messages: [] }, output: { system: ["<available_skills><skill><name>customize-opencode</name></skill></available_skills>"], model_messages: [] }, transforms: [{ name: "MessageV2.toModelMessagesEffect" }], source_refs: prompt ? ["prompt:" + prompt.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "skill-request-v48-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "skill-request-v48-case", "trace.json"), "utf8")) as any
    const skillRecord = trace.records.find((record: any) => record.event_type === "skill.load")

    expect(skillRecord.data).toMatchObject({
      skill_name: "repo-audit",
      request_source: "user_prompt",
      request_status: "missing",
    })
    expect(skillRecord.data.available_skill_names).toContain("customize-opencode")
    expect(skillRecord.data.quality_flags).toContain("skill_request_unresolved")
    expect(trace.metrics.trace_health.skill_request_unresolved).toBe(1)
  })

  test("closes missing skill tool and skill.load records as errors", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v49-skill-error-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "skill-error-v49.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const tool = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "skill", input: { args: { name: "repo-audit" }, callID: "call_skill" } })`,
        `CaseTrace.get()?.startSpan({ component: "skill", operation: "load", name: "repo-audit", input: { name: "repo-audit", callID: "call_skill" } })`,
        `const error = new Error('Skill "repo-audit" not found. Available skills: customize-opencode')`,
        `tool?.end({ status: "error", error, output: { error: error.message } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "skill-error-v49-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "skill-error-v49-case", "trace.json"), "utf8")) as any
    const skillTool = trace.records.find((record: any) => record.event_type === "tool.call" && record.title === "skill")
    const skillLoad = trace.records.find(
      (record: any) => record.event_type === "skill.load" && record.title === "repo-audit",
    )

    expect(skillTool.status).toBe("error")
    expect(skillTool.data.request_status).toBe("missing")
    expect(skillTool.data.skill_name).toBe("repo-audit")
    expect(skillTool.data.quality_flags).toContain("skill_request_unresolved")
    expect(skillLoad.status).toBe("error")
    expect(skillLoad.data.request_status).toBe("missing")
    expect(skillLoad.data.available_skill_names).toContain("customize-opencode")
    expect(skillLoad.data.finalized_status).toBeUndefined()
    expect(trace.metrics.trace_health.skill_request_unresolved).toBe(1)
    expect(trace.metrics.trace_health.unexpected_missing_close_records).toBe(0)
  })

  test("writes v6.0 lifecycle provenance records for LLM turns, exit gates, semantic evidence, and response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v45-lifecycle-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "lifecycle-v45.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "locate discount calculation" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_v45", agent: "build", model: { providerID: "deepseek", id: "unit-test" }, message_count: 1, system_count: 1, tool_count: 1 } })`,
        `const ctx = CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "unit-test", agent: "build", message_count: 1, messages: [{ role: "user", content: "locate discount calculation" }], system: ["system prompt"], tools: { grep: { description: "search files" } } })`,
        `CaseTrace.llmTurn({ turn_id: "turn_1", span_id: span?.id, session_id: "ses_v45", message_id: "msg_user", agent: "build", agent_role: "main", provider_id: "deepseek", model_id: "unit-test", status: "running", input_context_refs: ctx ? ["context_snapshot:" + ctx.snapshot_id] : [] })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_v45", message_id: "msg_user", agent: "build", phase: "turn.started", status: "running", summary: { step: 1, goal: "locate discount calculation" } })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "src/pricing.mjs defines applyDiscount", data: { path: "src/pricing.mjs", line_start: 7, line_end: 11, snippet: "export function applyDiscount(total, percent) { return total * (1 - percent) }" }, source_refs: span ? ["span:" + span.id] : [] })`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "applyDiscount is implemented in src/pricing.mjs lines 7-11", data: { path: "src/pricing.mjs", line_start: 7, line_end: 11, symbol: "applyDiscount" }, source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "applyDiscount is implemented in src/pricing.mjs lines 7-11.", source_refs: fact && ctx && span ? ["evidence:" + fact.node_id, "context_snapshot:" + ctx.snapshot_id, "tool_span:" + span.id] : [] })`,
        `CaseTrace.exitGate({ session_id: "ses_v45", message_id: "msg_assistant", has_final_answer: true, needs_compaction: false, auto_continue: false, synthetic_continue: false, continuation_source: "none", decision: "exit", reason: "assistant_finished_without_pending_tools", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_v45", message_id: "msg_assistant", agent: "build", phase: "response.completed", status: "success", summary: { final_answer: true } })`,
        `CaseTrace.llmTurn({ turn_id: "turn_1", span_id: span?.id, session_id: "ses_v45", message_id: "msg_assistant", agent: "build", agent_role: "main", provider_id: "deepseek", model_id: "unit-test", status: "success", duration_ms: 123, finish_reason: "stop", token_usage: { inputTokens: 12, outputTokens: 4, totalTokens: 16 }, request_id: "req_unit" })`,
        `span?.end({ output: { completed: true, finish_reason: "stop", request_id: "req_unit" }, tokenUsage: { inputTokens: 12, outputTokens: 4, totalTokens: 16 } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "lifecycle-v45-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "lifecycle-v45-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const eventTypes = trace.records.map((record: any) => record.event_type)

    expect(trace.trace_version).toBe("6.0")
    expect(eventTypes).toContain("llm.turn")
    expect(eventTypes).toContain("agent.lifecycle")
    expect(eventTypes).toContain("exit.gate")
    expect(eventTypes).toContain("evidence.semantic_fact")
    const evidenceFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.canonical_subject === "applyDiscount",
    )
    expect(evidenceFact.data.fact_kind).toBe("code_reference")
    expect(evidenceFact.data.canonical_subject).toBe("applyDiscount")
    expect(evidenceFact.data.claim).toBe("applyDiscount is implemented in src/pricing.mjs lines 7-11")
    expect(evidenceFact.data.structured_claim).toMatchObject({
      subject: "applyDiscount",
      predicate: "located_at",
      value: "src/pricing.mjs",
      extraction_method: "source_location_fields",
    })
    expect(evidenceFact.data.support_level).toBe("direct")
    expect(evidenceFact.data.quality_flags).toContain("path_only_evidence_fact")
    const llmTurn = trace.records.find((record: any) => record.event_type === "llm.turn")
    expect(llmTurn.status).toBe("success")
    expect(llmTurn.duration_ms).toBe(123)
    expect(llmTurn.token_usage.total).toBe(16)
    expect(llmTurn.data.finish_reason).toBe("stop")
    const exitGate = trace.records.find((record: any) => record.event_type === "exit.gate")
    expect(exitGate.data).toMatchObject({
      has_final_answer: true,
      needs_compaction: false,
      auto_continue: false,
      continuation_source: "none",
      decision: "exit",
      reason: "assistant_finished_without_pending_tools",
    })
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    expect(response.data.direct_evidence_refs).toHaveLength(1)
    expect(response.data.context_refs[0].startsWith("context_snapshot:")).toBe(true)
    expect(response.data.execution_refs[0].startsWith("tool_span:")).toBe(true)
    const responseClaim = trace.records.find((record: any) => record.event_type === "response.claim")
    expect(responseClaim.data.direct_evidence_refs).toHaveLength(1)
    expect(responseClaim.data.matched_evidence_refs).toHaveLength(1)
    expect(responseClaim.data.match_strategy).toBe("structured_text_overlap")
    expect(responseClaim.data.support_level).toBe("direct")
    expect(trace.dataflow_edges.filter((edge: any) => edge.relation === "supported_response")).toHaveLength(1)
    expect(trace.dataflow_edges.some((edge: any) => edge.relation === "supports_claim")).toBe(true)
    expect(trace.metrics.trace_health.circular_reference_markers).toBe(0)
    expect(html).toContain("LLM Turns")
    expect(html).toContain("Lifecycle And Exit Gates")
    expect(html).toContain("Semantic Evidence")
    expect(html).toContain("Trace Health")
    expect(html).toContain("applyDiscount")
  })

  test("keeps code paths, function calls, decimals, and percentages intact when splitting response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v47-claims-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "claim-precision-v47.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "修复点在 src/pricing.mjs：renewalQuote 使用 Math.min(input.discountPercent, 0.15) 将折扣上限限制为 15%。基线断言显示 48000 !== 51000。" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "claim-precision-v47-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "claim-precision-v47-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")

    expect(claims).toHaveLength(2)
    expect(claimText).toContain("src/pricing.mjs")
    expect(claimText).toContain("Math.min(input.discountPercent, 0.15)")
    expect(claimText).toContain("15%")
    expect(claimText).toContain("48000 !== 51000")
    expect(claims.filter((record: any) => String(record.data.text).includes("48000 !== 51000"))).toHaveLength(1)
    expect(claimText).not.toMatch(/(^|\n)(0\.|15\)|mjs)[。.!?；;]?($|\n)/)
    expect(trace.metrics.trace_health.broken_claim_fragments).toBe(0)
  })

  test("keeps parenthetical abbreviations inside one complete response claim", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-parenthetical-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "parenthetical-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const text =
      "When _cstack handled a non-Model right operand (i.e., a pre-computed separability matrix from a nested compound model), it used the wrong shape. The fix preserves the nested matrix."

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: ${JSON.stringify(text)} })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "parenthetical-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "parenthetical-claim-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimTexts = claims.map((record: any) => record.data.text)
    const responseClaimNodes = trace.nodes.filter((node: any) => node.kind === "response.claim")
    const events = (await fs.readFile(path.join(dir, "parenthetical-claim-case", "events.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    const traceClaim = events.find((event: any) => event.type === "semantic.response_claim")
    const traceClaims = events.filter((event: any) => event.type === "semantic.response_claim")
    const firstClaimKey = traceClaims[0]!.data.claim_key
    const firstNextClaimKey = traceClaims[0]!.data.next_claim_key
    const secondClaimKey = traceClaims[1]!.data.claim_key
    const secondPreviousClaimKey = traceClaims[1]!.data.previous_claim_key

    expect(claimTexts).toEqual([
      "When _cstack handled a non-Model right operand (i.e., a pre-computed separability matrix from a nested compound model), it used the wrong shape.",
      "The fix preserves the nested matrix.",
    ])
    expect(traceClaim.data).toMatchObject({
      claim_index: 1,
      claim_count: 2,
      claim_group_id: expect.stringMatching(/^claim_group_[a-f0-9]{12}$/),
      source_byte_range: [0, Buffer.byteLength(claimTexts[0])],
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
      next_claim_key: expect.any(String),
    })
    const [firstClaim, secondClaim] = claims
    const firstRecordRef = `record:${firstClaim!.record_id}`
    const secondRecordRef = `record:${secondClaim!.record_id}`
    const firstClaimNode = responseClaimNodes.find((node: any) => node.node_id === firstClaim!.record_id)
    const secondClaimNode = responseClaimNodes.find((node: any) => node.node_id === secondClaim!.record_id)
    const claimGroupEdges = trace.dataflow_edges.filter((edge: any) => edge.relation === "response_to_claim_group")
    const claimOrderEdges = trace.dataflow_edges.filter((edge: any) => edge.relation === "claim_group_precedes")

    expect(claims).toHaveLength(2)
    for (const claim of claims) {
      const node = responseClaimNodes.find((item: any) => item.node_id === claim.record_id)
      expectResponseClaimAtomizationClosure(claim.data)
      expectResponseClaimAtomizationClosure(node.data)
      expectResponseClaimAtomizationClosure(node.metadata)
    }
    expect(firstClaim!.data.next_claim_ref).toBe(secondRecordRef)
    expect(secondClaim!.data.previous_claim_ref).toBe(firstRecordRef)
    expect(firstClaimNode.data).toMatchObject({
      claim_group_id: firstClaim!.data.claim_group_id,
      claim_count: 2,
      source_byte_range: [0, Buffer.byteLength(claimTexts[0])],
      next_claim_ref: secondRecordRef,
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    })
    expect(firstClaimNode.metadata).toMatchObject({
      claim_group_id: firstClaim!.data.claim_group_id,
      claim_count: 2,
      source_byte_range: [0, Buffer.byteLength(claimTexts[0])],
      next_claim_ref: secondRecordRef,
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    })
    expect(secondClaimNode.data).toMatchObject({
      claim_group_id: secondClaim!.data.claim_group_id,
      claim_count: 2,
      source_byte_range: [Buffer.byteLength(`${claimTexts[0]} `), Buffer.byteLength(text)],
      previous_claim_ref: firstRecordRef,
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    })
    expect(secondClaimNode.metadata).toMatchObject({
      claim_group_id: secondClaim!.data.claim_group_id,
      claim_count: 2,
      source_byte_range: [Buffer.byteLength(`${claimTexts[0]} `), Buffer.byteLength(text)],
      previous_claim_ref: firstRecordRef,
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    })
    expect(claimGroupEdges).toHaveLength(2)
    expect(claimGroupEdges.map((edge: any) => edge.metadata.claim_group_id).sort()).toEqual(
      [firstClaim!.data.claim_group_id, secondClaim!.data.claim_group_id].sort(),
    )
    expect(claimOrderEdges).toHaveLength(1)
    expect(claimOrderEdges[0]).toMatchObject({
      from: { type: "response_claim", id: firstClaim!.record_id },
      to: { type: "response_claim", id: secondClaim!.record_id },
      eligible_for_attribution: false,
      label: "Adjacent claim/group order within a response segment",
      metadata: {
        causal_semantics: "claim_group_order_only",
        eligible_for_attribution: false,
        behavior_impact: "none",
      },
    })
    const causalOrderEdge = trace.edges.find((edge: any) => edge.edge_id === claimOrderEdges[0]!.edge_id)
    expect(causalOrderEdge).toMatchObject({
      eligible_for_attribution: false,
      evidence_refs: [],
      metadata: {
        causal_semantics: "claim_group_order_only",
        eligible_for_attribution: false,
        behavior_impact: "none",
      },
    })
    expect(causalOrderEdge).not.toHaveProperty("input_refs")
    expect(causalOrderEdge).not.toHaveProperty("source_refs")
    expect(causalOrderEdge).not.toHaveProperty("temporal_advisory_refs")
    const caseDir = path.join(dir, "parenthetical-claim-case")
    const journal = await readCausalIRJournal(caseDir)
    assertJournalReplaysCanonicalTrace(journal, trace)
    expect(firstNextClaimKey).toEqual(expect.any(String))
    expect(firstNextClaimKey).toBe(secondClaimKey)
    expect(secondPreviousClaimKey).toBe(firstClaimKey)
  })

  test("normalizes legacy runtime response claims into complete atomization facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-legacy-response-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "legacy-response-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const text = "Legacy 😀 claim. ".repeat(600)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ segment_id: "legacy_segment", response_role: "intermediate_summary", text: ${JSON.stringify(text)} })`,
        `CaseTrace.get()?.responseClaim({ claim_id: "legacy_claim", response_segment_id: "legacy_segment", text: ${JSON.stringify(text)}, claim_index: 1, metadata: { response_node_id: "responsenode_legacy_segment" } } as any)`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "legacy-response-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "legacy-response-claim-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const node = trace.nodes.find((item: any) => item.node_id === claim.record_id)

    expectResponseClaimAtomizationClosure(claim.data)
    expectResponseClaimAtomizationClosure(node.data)
    expectResponseClaimAtomizationClosure(node.metadata)
    expect(claim.data).toMatchObject({
      claim_group_id: `claim_group_${createHash("sha256")
        .update(["legacy_response_claim", "legacy_segment", "legacy_claim", text].join("\0"))
        .digest("hex")
        .slice(0, 16)}`,
      claim_count: 1,
      source_byte_range: [0, Buffer.byteLength(text)],
      atomization_status: "group_required",
      atomization_reason: "legacy_response_claim_missing_atomization_facts",
    })
    expect(node.data).toMatchObject(claim.data)
    expect(node.metadata).toMatchObject({
      claim_group_id: claim.data.claim_group_id,
      claim_count: 1,
      source_byte_range: [0, Buffer.byteLength(text)],
      atomization_status: "group_required",
      atomization_reason: "legacy_response_claim_missing_atomization_facts",
    })
    assertJournalReplaysCanonicalTrace(await readCausalIRJournal(caseDir), trace)
  })

  test("does not connect explicit claims across response segments", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-claim-group-boundaries-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "claim-group-boundaries.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ segment_id: "segment_one", response_role: "intermediate_summary", text: "First response segment." })`,
        `CaseTrace.responseOutput({ segment_id: "segment_two", response_role: "intermediate_summary", text: "Second response segment." })`,
        `const trace = CaseTrace.get()`,
        `trace?.responseClaim({ claim_id: "claim_one", response_segment_id: "segment_one", text: "First independent claim.", claim_group_id: "group_one", claim_index: 1, claim_count: 1, source_byte_range: [0, 24], atomization_status: "atomic", atomization_reason: "complete_merged_statement", metadata: { response_node_id: "responsenode_segment_one" } })`,
        `trace?.responseClaim({ claim_id: "claim_two", response_segment_id: "segment_two", text: "Second independent claim.", claim_group_id: "group_two", claim_index: 1, claim_count: 1, source_byte_range: [0, 25], atomization_status: "atomic", atomization_reason: "complete_merged_statement", metadata: { response_node_id: "responsenode_segment_two" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "claim-group-boundaries-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "claim-group-boundaries-case", "trace.json"), "utf8"),
    ) as any
    const claimGroupEdges = trace.dataflow_edges.filter((edge: any) => edge.relation === "response_to_claim_group")
    const claimOrderEdges = trace.dataflow_edges.filter((edge: any) => edge.relation === "claim_group_precedes")

    expect(claimGroupEdges).toHaveLength(2)
    expect(trace.records.filter((record: any) => record.event_type === "response.claim")).toHaveLength(2)
    expect(claimOrderEdges).toEqual([])
  })

  test("does not connect a single generated final-response claim", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-single-final-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "single-final-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ response_role: "final_answer", text: "The focused tests pass." })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "single-final-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "single-final-claim-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any

    expect(trace.records.filter((record: any) => record.event_type === "response.claim")).toHaveLength(1)
    expect(trace.dataflow_edges.filter((edge: any) => edge.relation === "claim_group_precedes")).toEqual([])
    assertJournalReplaysCanonicalTrace(await readCausalIRJournal(caseDir), trace)
  })

  test("atomizes a long final response from original text while retaining its summary artifact", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-long-final-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "long-final-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const firstClaim = `Completed ${"x".repeat(2100)}.`
    const secondClaim = "All 11 tests pass."
    const text = `${firstClaim} ${secondClaim}`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ response_role: "final_answer", text: ${JSON.stringify(text)} })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "long-final-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "long-final-claim-case", "trace.json"), "utf8")) as any
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const events = (await fs.readFile(path.join(dir, "long-final-claim-case", "events.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
      .filter((event: any) => event.type === "semantic.response_claim")

    expect(response.data.text).toMatchObject({ artifact_id: expect.any(String), preview: firstClaim.slice(0, 2048) })
    expect(claims).toHaveLength(2)
    expect(claims[0]!.data.text).toMatchObject({ artifact_id: expect.any(String) })
    expect(claims[1]!.data.text).toBe(secondClaim)
    expect(events).toHaveLength(2)
    const [start, end] = events[1]!.data.source_byte_range
    expect(Buffer.from(text).subarray(start, end).toString()).toBe(secondClaim)
    expect(events[1]!.data.raw_text).toMatchObject({ preview: secondClaim })
  })

  test("clears retained response sources from every finish lifecycle exit", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-response-source-cleanup-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "response-source-cleanup.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `import fs from "node:fs"`,
        `import path from "node:path"`,
        `const results = []`,
        `const responseText = "All 11 tests pass. " + "artifact detail ".repeat(220)`,
        `const sourceCount = (trace: any) => trace.responseSourceBySegmentID.size`,
        `const read = (file: string) => fs.existsSync(file) ? fs.readFileSync(file, "utf8") : undefined`,
        `const publicSnapshot = (caseID: string) => {`,
        `  const caseDir = path.join(${JSON.stringify(dir)}, caseID)`,
        `  const events = read(path.join(caseDir, "events.jsonl")) ?? ""`,
        `  const eventLines = events.trim().split("\\n").filter(Boolean)`,
        `  const summaryRecord = eventLines.findLast((line) => JSON.parse(line).type === "trace.finish")`,
        `  const persisted = ["manifest.json", "trace.json", "partial/latest.json", "legacy-trace.json"].map((file) => [file, read(path.join(caseDir, file))])`,
        `  const artifactDir = path.join(caseDir, "artifacts")`,
        `  const artifactFiles = fs.existsSync(artifactDir) ? fs.readdirSync(artifactDir, { recursive: true }).filter((file) => fs.statSync(path.join(artifactDir, file.toString())).isFile()).map((file) => [file.toString(), read(path.join(artifactDir, file.toString()))]).sort((left, right) => left[0].localeCompare(right[0])) : []`,
        `  const artifactRefs = Array.from(new Set((persisted.map((item) => item[1]).join("\\n") + events).match(/artifact_[a-z0-9_]+/g) ?? [])).sort()`,
        `  return { manifest: persisted[0][1], trace: persisted[1][1], partial: persisted[2][1], summary: persisted[3][1], summaryRecord, artifactRefs, artifactFiles, records: read(path.join(caseDir, "records.jsonl")), events }`,
        `}`,
        `const finalTrace = CaseTrace.get() as any`,
        `CaseTrace.responseOutput({ text: responseText })`,
        `CaseTrace.finish({ status: "success" })`,
        `results.push({ kind: "final", count: sourceCount(finalTrace), finished: finalTrace.finished })`,
        `const repeatBefore = publicSnapshot("response-source-final")`,
        `CaseTrace.finish({ status: "success" })`,
        `const repeatAfter = publicSnapshot("response-source-final")`,
        `results.push({ kind: "repeat", count: sourceCount(finalTrace), finished: finalTrace.finished })`,
        `CaseTrace.configure({ caseID: "response-source-non-final" })`,
        `const nonFinalTrace = CaseTrace.get() as any`,
        `CaseTrace.responseOutput({ response_role: "intermediate_summary", text: responseText })`,
        `CaseTrace.finish({ status: "success" })`,
        `results.push({ kind: "non_final", count: sourceCount(nonFinalTrace), finished: nonFinalTrace.finished })`,
        `CaseTrace.configure({ caseID: "response-source-cancelled" })`,
        `const cancelledTrace = CaseTrace.get() as any`,
        `CaseTrace.responseOutput({ text: responseText })`,
        `CaseTrace.finish({ status: "cancelled" })`,
        `results.push({ kind: "cancelled", count: sourceCount(cancelledTrace), finished: cancelledTrace.finished })`,
        `CaseTrace.configure({ caseID: "response-source-exception" })`,
        `CaseTrace.responseOutput({ text: responseText })`,
        `const trace = CaseTrace.get() as any`,
        `CaseTrace.finish({ status: "success" })`,
        `const exceptionBefore = publicSnapshot("response-source-exception")`,
        `trace.finished = false`,
        `trace.responseSourceBySegmentID.set("forced-segment", responseText)`,
        `trace.emitFinalResponseClaims = () => { throw new Error("forced claim emission failure") }`,
        `try { trace.finish({ status: "success" }) } catch {}`,
        `const exceptionAfter = publicSnapshot("response-source-exception")`,
        `results.push({ kind: "exception", count: sourceCount(trace), finished: trace.finished })`,
        `trace.finished = true`,
        `process.stdout.write(JSON.stringify({ results, repeatBefore, repeatAfter, exceptionBefore, exceptionAfter }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "response-source-final",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })

    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    const lifecycle = JSON.parse(await new Response(proc.stdout).text())
    expect(lifecycle.results).toEqual([
      { kind: "final", count: 0, finished: true },
      { kind: "repeat", count: 0, finished: true },
      { kind: "non_final", count: 0, finished: true },
      { kind: "cancelled", count: 0, finished: true },
      { kind: "exception", count: 0, finished: false },
    ])
    expect(lifecycle.repeatBefore.manifest).toBeDefined()
    expect(lifecycle.repeatBefore.trace).toBeDefined()
    expect(lifecycle.repeatBefore.partial).toBeDefined()
    expect(lifecycle.repeatBefore.summaryRecord).toBeDefined()
    expect(lifecycle.repeatBefore.artifactRefs.length).toBeGreaterThan(0)
    expect(lifecycle.repeatAfter.manifest).toBe(lifecycle.repeatBefore.manifest)
    expect(lifecycle.repeatAfter.trace).toBe(lifecycle.repeatBefore.trace)
    expect(lifecycle.repeatAfter.partial).toBe(lifecycle.repeatBefore.partial)
    expect(lifecycle.repeatAfter.summary).toBe(lifecycle.repeatBefore.summary)
    expect(lifecycle.repeatAfter.summaryRecord).toEqual(lifecycle.repeatBefore.summaryRecord)
    expect(lifecycle.repeatAfter.artifactRefs).toEqual(lifecycle.repeatBefore.artifactRefs)
    expect(lifecycle.repeatAfter.artifactFiles).toEqual(lifecycle.repeatBefore.artifactFiles)
    for (const field of ["manifest", "trace", "partial", "summary", "summaryRecord", "records", "events"]) {
      expect(lifecycle.exceptionBefore[field]).toEqual(expect.any(String))
      expect(lifecycle.exceptionBefore[field].length).toBeGreaterThan(0)
    }
    expect(lifecycle.exceptionBefore.artifactRefs.length).toBeGreaterThan(0)
    expect(lifecycle.exceptionBefore.artifactFiles.length).toBeGreaterThan(0)
    expect(lifecycle.exceptionAfter.manifest).toBe(lifecycle.exceptionBefore.manifest)
    expect(lifecycle.exceptionAfter.trace).toBe(lifecycle.exceptionBefore.trace)
    expect(lifecycle.exceptionAfter.partial).toBe(lifecycle.exceptionBefore.partial)
    expect(lifecycle.exceptionAfter.summary).toBe(lifecycle.exceptionBefore.summary)
    expect(lifecycle.exceptionAfter.summaryRecord).toEqual(lifecycle.exceptionBefore.summaryRecord)
    expect(lifecycle.exceptionAfter.artifactRefs).toEqual(lifecycle.exceptionBefore.artifactRefs)
    expect(lifecycle.exceptionAfter.artifactFiles).toEqual(lifecycle.exceptionBefore.artifactFiles)
    expect(lifecycle.exceptionAfter.records.startsWith(lifecycle.exceptionBefore.records)).toBe(true)
    expect(lifecycle.exceptionAfter.events.startsWith(lifecycle.exceptionBefore.events)).toBe(true)

    const readCaseOutputs = async (caseID: string) => {
      const caseDir = path.join(dir, caseID)
      return {
        trace: JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any,
        manifest: JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any,
        legacy: JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any,
        partial: JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any,
      }
    }
    const final = await readCaseOutputs("response-source-final")
    const nonFinal = await readCaseOutputs("response-source-non-final")
    const cancelled = await readCaseOutputs("response-source-cancelled")

    for (const output of [final, nonFinal, cancelled]) {
      expect(output.trace.manifest).toEqual(output.manifest)
      expect(output.partial.manifest).toEqual(output.manifest)
      expect(output.legacy.status).toBe(output.manifest.status)
      expect(output.trace.artifacts).toEqual(output.partial.artifacts)
      expect(output.trace.metrics).toBeDefined()
      const response = output.trace.records.find((record: any) => record.event_type === "response.output")
      expect(response.data.text.artifact_id).toEqual(expect.any(String))
      expect(
        output.trace.artifacts.some((artifact: any) => artifact.artifact_id === response.data.text.artifact_id),
      ).toBe(true)
    }
    expect(final.manifest).toMatchObject({ status: "success", case_status: "success" })
    expect(final.trace.records.filter((record: any) => record.event_type === "response.claim").length).toBeGreaterThan(
      0,
    )
    expect(nonFinal.manifest).toMatchObject({ status: "success", case_status: "success" })
    expect(nonFinal.trace.records.filter((record: any) => record.event_type === "response.claim")).toHaveLength(0)
    expect(cancelled.manifest).toMatchObject({ status: "cancelled", case_status: "cancelled" })
    expect(cancelled.trace.records.filter((record: any) => record.event_type === "response.claim")).toHaveLength(0)
  })

  test("extracts explicit discount cap values without defaulting unrelated cap lines to 15 percent", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-discount-values-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "discount-values-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "payment discount cap", data: { path: "src/payment/discounts.mjs", line_start: 3, line_end: 3, text: "export const settlementDiscountCap = 0.2" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "old design discount cap", data: { path: "docs/old-design.md", line_start: 8, line_end: 8, text: "Legacy renewal discount cap is 20 percent." } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "current requirement discount cap", data: { path: "docs/current-requirement.md", line_start: 4, line_end: 4, text: "Current renewal discount cap is 15%." } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "discount-values-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "discount-values-v56-case", "trace.json"), "utf8")) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const values = facts.map((record: any) => record.data.structured_claim?.value)

    expect(values).toContain("0.2")
    expect(values).toContain("20 percent")
    expect(values).toContain("15%")
    expect(values.filter((value: string) => value === "15 percent")).toHaveLength(0)
  })

  test("extracts implementation entry path from documentation text instead of the evidence file path", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v62-entry-path-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "entry-path-v62.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "read", category: "tool_output", summary: "docs/architecture.md", data: { path: "docs/architecture.md", line_start: 3, line_end: 3, text: "The active implementation entry point is \`src/pricing.mjs\`." } })`,
        `CaseTrace.evidenceFact({ source: "read", category: "tool_output", summary: "src/pricing.mjs", data: { path: "src/pricing.mjs", line_start: 1, line_end: 1, text: "export function renewalQuote(input) {" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "entry-path-v62-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "entry-path-v62-case", "trace.json"), "utf8")) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const docFact = facts.find((record: any) => record.data.source_locations?.[0]?.path === "docs/architecture.md")
    const codeFact = facts.find((record: any) => record.data.source_locations?.[0]?.path === "src/pricing.mjs")

    expect(docFact.data.structured_claim).toMatchObject({
      predicate: "implementation_entry",
      value: "src/pricing.mjs",
    })
    expect(docFact.data.structured_claim.source_span.path).toBe("docs/architecture.md")
    expect(codeFact.data.structured_claim).toMatchObject({
      predicate: "implementation_entry",
      value: "src/pricing.mjs",
    })
  })

  test("uses the numeric value nearest to discount cap wording in long evidence text", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-cap-nearest-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "cap-nearest-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "architecture overview", data: { path: "docs/architecture.md", line_start: 3, line_end: 3, text: "Two discounts can apply -- a 10% loyalty discount and a 5% volume discount. The total renewal discount cap must be 15 percent." } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "cap-nearest-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "cap-nearest-v56-case", "trace.json"), "utf8")) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const values = facts.map((record: any) => record.data.structured_claim?.value)

    expect(values).toContain("15 percent")
    expect(values).not.toContain("10%")
    expect(values).not.toContain("5%")
  })

  test("does not create response claims from markdown headings or code fence markers", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-fence-filter-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "fence-filter-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "### MCP 返回的事实\\n\\n\\\`\\\`\\\`diff\\n- const cap = 0.2\\n+ const cap = 0.15\\n\\\`\\\`\\\`\\n\\n## 修复完成\\n\\n### 修改点\\n\\nsrc/pricing.mjs 将 discount cap 调整为 15%。" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "fence-filter-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "fence-filter-v56-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")

    expect(claimText).not.toContain("MCP 返回的事实")
    expect(claimText).not.toContain("const cap =")
    expect(claimText).not.toContain("```diff")
    expect(claimText).not.toContain("修复完成")
    expect(claimText).not.toContain("### 修改点")
    expect(claimText).toContain("src/pricing.mjs")
    expect(claimText).toContain("discount cap")
    expect(claimText).toContain("15%")
  })

  test("links failed cases to finalized running LLM records for backward taint traversal", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-failed-source-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "failed-source-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "run slow provider case" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_failed", agent: "build", model: { providerID: "deepseek", id: "unit-test" }, message_count: 1, system_count: 1, tool_count: 1 } })`,
        `CaseTrace.llmTurn({ turn_id: span?.id, span_id: span?.id, session_id: "ses_failed", message_id: "msg_user", agent: "build", agent_role: "main", provider_id: "deepseek", model_id: "unit-test", status: "running" })`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGTERM", signal: "SIGTERM" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "failed-source-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "failed-source-v56-case", "trace.json"), "utf8")) as any
    const failed = trace.records.find((record: any) => record.event_type === "case.failed")
    const llmTurn = trace.records.find((record: any) => record.event_type === "llm.turn")

    expect(llmTurn.data.finalized_reason).toBe("trace_cancelled")
    expect(failed.source_refs).toContain(`node:${llmTurn.record_id}`)
    expect(failed.data.finalized_open_record_refs).toContain(`node:${llmTurn.record_id}`)
    expect(trace.dataflow_edges.some((edge: any) => edge.relation === "failed_before")).toBe(true)
    expect(trace.metrics.trace_health.llm_turns_missing_token_usage).toBe(0)
    expect(trace.metrics.trace_health.llm_turns_missing_finish_reason).toBe(0)
    expect(trace.metrics.trace_health.issues.map((issue: any) => issue.kind)).not.toContain(
      "llm_turn_missing_token_usage",
    )
    expect(trace.metrics.trace_health.issues.map((issue: any) => issue.kind)).not.toContain(
      "llm_turn_missing_finish_reason",
    )
  })

  test("marks subagent summary facts as secondary evidence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v57-secondary-fact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "secondary-fact-v57.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "subagent", category: "explore", summary: "Subagent summary says docs/architecture.md reports renewal discount cap is 20 percent.", data: { output: "Subagent summary says docs/architecture.md reports renewal discount cap is 20 percent." }, source_refs: ["span:subagent_span"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "secondary-fact-v57-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "secondary-fact-v57-case", "trace.json"), "utf8")) as any
    const fact = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")

    expect(fact.data.structured_claim.value).toBe("20 percent")
    expect(fact.data.support_level).toBe("context")
    expect(fact.data.evidence_origin).toBe("secondary_summary")
    expect(fact.data.quality_flags).toContain("secondary_source_fact")
    expect(fact.data.quality_flags).toContain("summary_derived_fact")
  })

  test("extracts multiple structured facts from one subagent result", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v62-subagent-multifact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-multifact-v62.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "subagent", category: "explore", summary: "Subagent result:\\n1. owner: billing-platform\\n2. discount cap: 15 percent\\n3. implementation entry: src/pricing.mjs", data: { output: "Subagent result:\\n1. owner: billing-platform\\n2. discount cap: 15 percent\\n3. implementation entry: src/pricing.mjs", child_session_id: "ses_child" }, source_refs: ["span:subagent_span"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-multifact-v62-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "subagent-multifact-v62-case", "trace.json"), "utf8"),
    ) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const predicates = facts.map((record: any) => record.data.structured_claim?.predicate)
    const values = facts.map((record: any) => record.data.structured_claim?.value)

    expect(predicates).toContain("owner")
    expect(predicates).toContain("discount_cap")
    expect(predicates).toContain("implementation_entry")
    expect(values).toContain("billing-platform")
    expect(values).toContain("15 percent")
    expect(values).toContain("src/pricing.mjs")
    expect(facts.every((record: any) => record.data.fact_kind === "subagent_result")).toBe(true)
    expect(facts.every((record: any) => record.data.evidence_origin === "secondary_summary")).toBe(true)
  })

  test("keeps task tool output as observation without duplicating subagent semantic facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v58-task-output-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "task-output-v58.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "task", category: "tool_output", summary: "Task result says renewal discount cap is 20 percent.", data: { output: "Task result says renewal discount cap is 20 percent.", child_session_id: "ses_child", subagent_type: "explore" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "task-output-v58-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "task-output-v58-case", "trace.json"), "utf8")) as any
    const observations = trace.records.filter((record: any) => record.event_type === "execution.observation")
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")

    expect(observations.some((record: any) => record.data.source === "task")).toBe(true)
    expect(facts).toHaveLength(0)
  })

  test("merges duplicate semantic facts while preserving source occurrences", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v59-duplicate-fact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "duplicate-fact-v59.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "read", category: "tool_output", summary: "docs/requirements.md", data: { path: "docs/requirements.md", text: "The renewal discount cap is 20 percent." }, source_refs: ["tool_result:read1"] })`,
        `CaseTrace.evidenceFact({ source: "read", category: "tool_output", summary: "docs/requirements.md", data: { path: "docs/requirements.md", text: "The renewal discount cap is 20 percent." }, source_refs: ["tool_result:read2"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "duplicate-fact-v59-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "duplicate-fact-v59-case", "trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(path.join(dir, "duplicate-fact-v59-case"))
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")

    expect(facts).toHaveLength(1)
    expect(facts[0].data.occurrence_count).toBe(2)
    expect(facts[0].source_refs).toContain("tool_result:read1")
    expect(facts[0].source_refs).toContain("tool_result:read2")
    expect(trace.metrics.trace_health.duplicate_semantic_facts).toBe(0)
    expect(journal.every((entry: any) => typeof entry.operation === "string")).toBe(true)
    expect(journal.map((entry: any) => entry.sequence)).toEqual(journal.map((_: any, index: number) => index + 1))
    expect(journal.some((entry: any) => entry.data?.kind === "evidence_duplicate_suppressed")).toBe(true)
  })

  test("does not treat missing compaction after-token estimate as zero", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v60-compaction-estimate-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-estimate-v60.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compaction({ trigger: "auto", context_limit: 4000, output_summary: "Keep the active discount cap constraint.", auto_continue: true, result: "continue", context_ledger: { algorithm: "head-tail-summary", retained_message_ids: ["msg_a"], dropped_message_ids: [] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-estimate-v60-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-estimate-v60-case", "trace.json"), "utf8"),
    ) as any
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(compaction.data.token_estimate_after).toBeUndefined()
    expect(compaction.data.compression_loss_risks).not.toContain("zero_token_estimate_after")
  })

  test("extracts preserved constraints and paths from compaction summaries", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v61-compaction-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-facts-v61.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "read", category: "requirements", summary: "The total renewal discount cap is 15 percent.", data: { subject: "renewalQuote", predicate: "discount_cap", value: "15 percent", path: "docs/architecture.md", text: "The total renewal discount cap is 15 percent." } })`,
        `CaseTrace.evidenceFact({ source: "read", category: "code_reference", summary: "Implementation entry is src/billing/pricing.mjs.", data: { subject: "renewalQuote", predicate: "implementation_entry", value: "src/billing/pricing.mjs", path: "src/billing/pricing.mjs", text: "export function renewalQuote(input) {}" } })`,
        `CaseTrace.compaction({ trigger: "auto", input_tokens: 2400, context_limit: 1200, output_summary: "## Constraints & Preferences\\n- Only modify files under src/billing/.\\n- Do not modify src/payment/.\\n- The total renewal discount cap is 15 percent.\\n\\n## Relevant Files\\n- docs/architecture.md\\n- src/billing/pricing.mjs", auto_continue: true, result: "continue", after_context_refs: ["message:continue"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-facts-v61-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-facts-v61-case", "trace.json"), "utf8"),
    ) as any
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const capFact = facts.find((record: any) => record.data.structured_claim?.value === "15 percent")
    const entryFact = facts.find((record: any) => record.data.structured_claim?.value === "src/billing/pricing.mjs")

    expect(compaction.data.summary_constraint_facts.join("\n")).toContain("Only modify files under src/billing/")
    expect(compaction.data.summary_constraint_facts.join("\n")).toContain("Do not modify src/payment/")
    expect(compaction.data.summary_constraint_facts.join("\n")).not.toContain("Constraints & Preferences")
    expect(compaction.data.summary_key_facts.join("\n")).toContain("15 percent")
    expect(compaction.data.summary_preserved_paths).toContain("src/billing/")
    expect(compaction.data.summary_preserved_paths).toContain("src/payment/")
    expect(compaction.data.summary_preserved_paths).toContain("docs/architecture.md")
    expect(capFact).toBeTruthy()
    expect(entryFact).toBeTruthy()
    expect(compaction.data.retained_fact_refs).toContain(`evidence:${capFact.record_id}`)
    expect(compaction.data.retained_fact_refs).toContain(`evidence:${entryFact.record_id}`)
    expect(compaction.data.context_ledger.retained_fact_refs).toContain(`evidence:${capFact.record_id}`)
    expect(compaction.data.context_ledger.quality_flags).toContain("retained_fact_refs_inferred_from_summary")
  })

  test("emits response claims only for the final user-visible answer", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v48-final-claims-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "final-claims-v48.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "## Goal\\n- Read docs/architecture.md and prepare an answer.", source_refs: ["context_snapshot:ctx_intermediate"] })`,
        `CaseTrace.responseOutput({ text: "Owner is billing-platform. Entry point is src/pricing.mjs. Discount cap is 15%.", source_refs: ["context_snapshot:ctx_final"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "final-claims-v48-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "final-claims-v48-case", "trace.json"), "utf8")) as any
    const responses = trace.records.filter((record: any) => record.event_type === "response.output")
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const finalResponse = responses.find((record: any) => record.data.is_final_for_case === true)

    expect(responses[0].data.response_role).toBe("intermediate_summary")
    expect(claims.length).toBeGreaterThan(0)
    expect(claims.every((record: any) => record.data.response_segment_id === finalResponse.data.segment_id)).toBe(true)
    expect(JSON.stringify(claims)).not.toContain("Read docs/architecture.md and prepare an answer")
    expect(trace.metrics.trace_health.non_final_response_claims).toBe(0)
  })

  test("filters non-factual final-answer lines and avoids malformed markdown source locations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v49-claim-filter-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "claim-filter-v49.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const ownerFact = CaseTrace.evidenceFact({ source: "mcp", category: "repo_fact", summary: "owner", data: { subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `const entryFact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "entry", data: { path: "docs/architecture.md", line_start: 5, line_end: 5, text: "The implementation entry point is src/pricing.mjs." } })`,
        `CaseTrace.responseOutput({ text: "Here's the summary:\\n\\n- **Owner**: \`billing-platform\` team (\`docs/architecture.md:3\`, \`src/pricing.mjs:10-12\`)\\n- **Entry point**: \`renewalQuote(input)\` function in \`src/pricing.mjs\`\\n\\nNo further steps needed.", source_refs: ownerFact && entryFact ? ["evidence:" + ownerFact.node_id, "evidence:" + entryFact.node_id, "context_snapshot:ctx_final"] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "claim-filter-v49-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "claim-filter-v49-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")
    const claimLocations = claims.flatMap((record: any) => record.source_locations ?? [])

    expect(claims).toHaveLength(2)
    expect(claimText).toContain("billing-platform")
    expect(claimText).toContain("renewalQuote(input)")
    expect(claimText).not.toContain("Here's the summary")
    expect(claimText).not.toContain("No further steps needed")
    expect(claimLocations.every((location: any) => !String(location.path ?? "").includes("**"))).toBe(true)
    expect(claimLocations.every((location: any) => !String(location.path ?? "").startsWith("`"))).toBe(true)
  })

  test("filters markdown headings, list ordinals, and table scaffolding before creating response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v50-markdown-filter-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "markdown-filter-v50.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "以下是最终报告：\\n\\n## 最终答案\\n\\n**总结：**\\n\\n**冲突总结：**\\n\\n| 来源 | 折扣上限 | 状态 |\\n|------|----------|------|\\n1. \`renewalQuote\` 负责人为 \`billing-platform\` 团队。\\n2. 实现入口为 \`src/pricing.mjs\` 中的 \`renewalQuote(input)\` 函数。\\n\\n## 设计约束\\n\\n| 约束 | 描述 |\\n|------|------|\\n| 忠诚折扣 | 使用年限 ≥ 3 年享 10% 折扣 |\\n\\n## 压缩链路验证汇总\\n\\n| 项目 | 结果 |\\n|------|------|\\n| **Owner** | \`billing-platform\` |\\n| **测试结果** | 全部通过（\`npm test\` -> \`pricing tests passed\`） |\\n\\n**计算推导**（两种输入一致）：\\n\\n| 输入 | 计算 | 结果 |\\n|------|------|------|\\n\\n## npm test 结果\\n\\n## 修改文件\\n\\n### 使用的上下文资料\\n\\n## 额外通用性检查\\n\\n### 额外通用性检查结果" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "markdown-filter-v50-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "markdown-filter-v50-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")

    expect(claimText).not.toContain("**总结")
    expect(claimText).not.toContain("以下是最终报告")
    expect(claimText).not.toContain("最终答案")
    expect(claimText).not.toContain("冲突总结")
    expect(claimText).not.toContain("1.")
    expect(claimText).not.toContain("2.")
    expect(claimText).not.toContain("设计约束")
    expect(claimText).not.toContain("约束: 描述")
    expect(claimText).not.toContain("压缩链路验证汇总")
    expect(claimText).not.toContain("| 来源 | 折扣上限 | 状态 |")
    expect(claimText).not.toContain("| 项目 | 结果 |")
    expect(claimText).not.toContain("|------|------|")
    expect(claimText).not.toContain("计算推导")
    expect(claimText).not.toContain("输入: 计算 | 结果")
    expect(claimText).not.toContain("npm test 结果")
    expect(claimText).not.toContain("修改文件")
    expect(claimText).not.toContain("使用的上下文资料")
    expect(claimText).not.toContain("额外通用性检查")
    expect(claimText).not.toContain("额外通用性检查结果")
    expect(claimText).toContain("billing-platform")
    expect(claimText).toContain("忠诚折扣")
    expect(claimText).toContain("renewalQuote(input)")
    expect(claimText).toContain("pricing tests passed")
  })

  test("drops emphasized conflict headings from final response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-conflict-heading-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "conflict-heading.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "**冲突点**\\n\\nThe active discount cap is 15%." })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "conflict-heading-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "conflict-heading-case", "trace.json"), "utf8")) as any
    const claimText = trace.records
      .filter((record: any) => record.event_type === "response.claim")
      .map((record: any) => record.data.text)
      .join("\n")

    expect(claimText).not.toContain("冲突点")
    expect(claimText).toContain("active discount cap")
  })

  test("keeps short verification conclusions as atomic response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-short-verification-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "short-verification-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "全部通过。\\n\\n## 测试脚本说明\\n\\n| 脚本 | 命令 | 覆盖风险 |\\n|---|---|---|\\n| owner.test.mjs | npm test | 模块归属 |" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "short-verification-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "short-verification-claim-case", "trace.json"), "utf8"),
    ) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")

    expect(claimText).toContain("全部通过。")
    expect(claimText).not.toContain("测试脚本说明")
    expect(claimText).not.toContain("脚本: 命令")
  })

  test("binds verification claims to the effective repository revision", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-revision-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "revision-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ verification_id: "baseline", command: "npm test", exit_code: 1, status: "failed", stderr: "1 test failed" })`,
        `const baselineFact = CaseTrace.evidenceFact({ fact_id: "baseline_result", source: "bash", category: "verification_output", summary: "baseline npm test failed", data: { command: "npm test", status: "failed", exit_code: 1 }, source_refs: ["verification:baseline"] })`,
        `CaseTrace.change({ change_id: "implementation", files: ["src/owner.mjs"], diff: "-return 'legacy'\\n+return 'billing-platform'" })`,
        `CaseTrace.verification({ verification_id: "post_change", command: "npm test", exit_code: 0, status: "passed", stdout: "all tests passed" })`,
        `const postChangeFact = CaseTrace.evidenceFact({ fact_id: "post_change_result", source: "bash", category: "verification_output", summary: "post-change npm test passed", data: { command: "npm test", status: "passed", exit_code: 0 }, source_refs: ["verification:post_change"] })`,
        `const setupFact = CaseTrace.evidenceFact({ fact_id: "setup_command", source: "bash", category: "verification_output", summary: "list repository files", data: { command: "ls -R", status: "passed", exit_code: 0 } })`,
        `CaseTrace.responseOutput({ text: "Before the change, the baseline npm test failed. All tests passed after the change.", source_refs: [baselineFact ? "evidence:" + baselineFact.node_id : "", postChangeFact ? "evidence:" + postChangeFact.node_id : "", setupFact ? "evidence:" + setupFact.node_id : "", "verification:baseline", "change:implementation", "verification:post_change"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "revision-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "12000",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "revision-claim-case", "trace.json"), "utf8")) as any
    const baselineFact = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.fact_id === "baseline_result",
    )
    const postChangeFact = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.fact_id === "post_change_result",
    )
    const setupFact = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.fact_id === "setup_command",
    )
    const currentClaim = trace.records.find(
      (record: any) => record.event_type === "response.claim" && String(record.data.text).includes("All tests passed"),
    )
    const historicalClaim = trace.records.find(
      (record: any) =>
        record.event_type === "response.claim" && String(record.data.text).includes("baseline npm test failed"),
    )
    const baselineRef = `evidence:${baselineFact.record_id}`
    const postChangeRef = `evidence:${postChangeFact.record_id}`
    const setupRef = `evidence:${setupFact.record_id}`

    expect(baselineFact.data.verification_refs).toEqual(["verification:baseline"])
    expect(baselineFact.data.verification_repository_revision).toBe(0)
    expect(baselineFact.data.verification_phase).toBe("baseline")
    expect(baselineFact.data.verification_status).toBe("failed")
    expect(baselineFact.data.verification_effective_for_final_state).toBe(false)
    expect(baselineFact.data.verification_temporal_role).toBe("superseded")
    expect(postChangeFact.data.verification_refs).toEqual(["verification:post_change"])
    expect(postChangeFact.data.verification_repository_revision).toBe(1)
    expect(postChangeFact.data.verification_phase).toBe("post_change")
    expect(postChangeFact.data.verification_status).toBe("passed")
    expect(postChangeFact.data.verification_effective_for_final_state).toBe(true)
    expect(postChangeFact.data.verification_temporal_role).toBe("current_effective")

    expect(currentClaim.data.claim_kind).toBe("verification")
    expect(currentClaim.data.temporal_scope).toBe("current_revision")
    expect(currentClaim.data.repository_revision).toBe(1)
    expect(currentClaim.data.direct_evidence_refs).toContain(postChangeRef)
    expect(currentClaim.data.direct_evidence_refs).not.toContain(baselineRef)
    expect(currentClaim.data.direct_evidence_refs).not.toContain(setupRef)
    expect(currentClaim.data.direct_support_refs).toContain("verification:post_change")
    expect(currentClaim.data.direct_support_refs).not.toContain("verification:baseline")
    expect(currentClaim.data.superseded_evidence_refs).toEqual(
      expect.arrayContaining(["verification:baseline", baselineRef]),
    )
    expect(currentClaim.data.grounding_decisions).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          candidate_ref: baselineRef,
          decision: "rejected_inapplicable",
          rejection_reason: "superseded_verification",
          candidate_effective_for_final_state: false,
          candidate_repository_revision: 0,
          candidate_verification_status: "failed",
          attribution_eligible: false,
        }),
        expect.objectContaining({
          candidate_ref: setupRef,
          decision: "rejected_inapplicable",
          rejection_reason: "unscoped_verification_candidate",
          attribution_eligible: false,
        }),
      ]),
    )
    expect(currentClaim.data.quality_flags).not.toContain("weak_evidence_match")

    expect(historicalClaim.data.temporal_scope).toBe("historical")
    expect(historicalClaim.data.direct_evidence_refs).toContain(baselineRef)
    expect(historicalClaim.data.direct_evidence_refs).not.toContain(postChangeRef)
    expect(historicalClaim.data.direct_support_refs).not.toContain("verification:post_change")

    const baselineToCurrentEdges = trace.edges.filter(
      (edge: any) => edge.from.ref_id === baselineFact.record_id && edge.to.ref_id === currentClaim.record_id,
    )
    expect(baselineToCurrentEdges).toEqual([
      expect.objectContaining({
        original_relation: "context_to_claim",
        normalized_relation: "contextualizes_claim",
        evidence_tier: "temporal_advisory",
        eligible_for_attribution: false,
        metadata: expect.objectContaining({ causal_semantics: "superseded_verification_context" }),
      }),
    ])
  })

  test("drops localized key-value table headers from response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v58-table-header-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "table-header-v58.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "| 字段 | 值 |\\n|------|----|\\n| subject | \`renewalQuote\` |\\n| predicate | \`discount_cap\` |\\n| value | **15%** |\\n| path | \`docs/current-requirement.md:3\` |" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "table-header-v58-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "table-header-v58-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")

    expect(claimText).not.toContain("字段: 值")
    expect(claimText).toContain("value: 15%")
    expect(claimText).toContain("path: docs/current-requirement.md:3")
  })

  test("canonicalizes factual markdown table rows and drops section scaffolding claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v51-table-claims-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "table-claims-v51.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "## 需求影响分析报告\\n\\n### 各来源的关键结论对比\\n\\n| 议题 | large-requirements.md | docs/architecture.md | src/pricing.mjs | syntheticFacts (MCP) |\\n|---|---|---|---|---|\\n| **Owner** | billing-platform | billing-platform | \`return \\\\\\"billing-platform\\\\\\"\` | billing-platform |\\n| **Discount cap** | 15% | 15% | \`Math.min(..., 0.2)\` -> 20% | 15% |\\n| **Implementation entry** | \`src/pricing.mjs\` | \`src/pricing.mjs\` | actual file \`src/pricing.mjs\` | - |\\n\\n### 子 Agent 独立总结\\n\\n子 agent 确认所有 2500 条需求 100% 一致。\\n\\n### 是否需要改动\\n\\n需要将 \`src/pricing.mjs:6\` 中的 \`0.2\` 修正为 \`0.15\`。" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "table-claims-v51-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "table-claims-v51-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const claimText = claims.map((record: any) => record.data.text).join("\n")
    const tableClaims = claims.filter((record: any) => record.data.claim_format === "table_fact")

    expect(claimText).not.toContain("需求影响分析报告")
    expect(claimText).not.toContain("各来源的关键结论对比")
    expect(claimText).not.toContain("子 Agent 独立总结")
    expect(claimText).not.toContain("是否需要改动")
    expect(claimText).not.toContain("| 议题 |")
    expect(claimText).not.toContain("|---|---|")
    expect(tableClaims.map((record: any) => record.data.table_subject)).toEqual([
      "Owner",
      "Discount cap",
      "Implementation entry",
    ])
    expect(tableClaims[0].data.raw_text).toContain("| **Owner** |")
    expect(tableClaims[0].data.canonical_text).toContain("Owner")
    expect(tableClaims[0].data.canonical_text).toContain("billing-platform")
    expect(tableClaims[0].data.table_cells).toContain("billing-platform")
    expect(tableClaims[1].data.canonical_text).toContain("20%")
    expect(tableClaims[2].data.canonical_text).toContain("src/pricing.mjs")
  })

  test("records explicit skill requests nested inside prompt parts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v50-skill-request-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "skill-request-v50.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const prompt = CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_skill", input: { parts: [{ type: "text", text: "请尝试使用 repo-audit skill（如果可用）审查该需求" }] }, output: { part_count: 1 } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_skill", agent: "build", provider_id: "deepseek", model_id: "unit-test", input: {}, output: { model_messages: [], tools: {} }, source_refs: prompt ? ["prompt:" + prompt.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "skill-request-v50-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "skill-request-v50-case", "trace.json"), "utf8")) as any
    const skill = trace.records.find((record: any) => record.event_type === "skill.load")

    expect(skill).toBeTruthy()
    expect(skill.data.skill_name).toBe("repo-audit")
    expect(skill.data.request_source).toBe("user_prompt")
    expect(skill.data.request_status).toBe("missing")
    expect(skill.data.quality_flags).toContain("skill_request_unresolved")
    expect(trace.metrics.trace_health.skill_request_unresolved).toBe(1)
  })

  test("keeps broad response refs as legacy context while narrowing direct claim evidence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v49-legacy-refs-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "legacy-refs-v49.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const ownerFact = CaseTrace.evidenceFact({ source: "mcp", category: "repo_fact", summary: "owner", data: { subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `const verificationFact = CaseTrace.evidenceFact({ source: "tool", category: "verification", summary: "tests passed", data: { command: "npm test", exit_code: 0, stdout: "pricing tests passed" } })`,
        `CaseTrace.responseOutput({ text: "Owner is billing-platform.", source_refs: ownerFact && verificationFact ? ["evidence:" + ownerFact.node_id, "evidence:" + verificationFact.node_id, "context_snapshot:ctx_final", "llm:llm_turn", "tool_span:span_tool"] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "legacy-refs-v49-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "legacy-refs-v49-case", "trace.json"), "utf8")) as any
    const verification = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "verification_output",
    )
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")

    expect(claim.data.direct_evidence_refs).not.toContain(`evidence:${verification.record_id}`)
    expect(claim.data.legacy_context_refs).toContain(`evidence:${verification.record_id}`)
    expect(claim.data.legacy_context_refs).toContain("context_snapshot:ctx_final")
    expect(claim.data.legacy_context_refs).toContain("llm:llm_turn")
    expect(claim.data.legacy_context_refs).toContain("tool_span:span_tool")
    expect(claim.data.quality_flags).not.toContain("over_attributed_claim")
  })

  test("does not match verification-output evidence to owner claims without test-result wording", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v48-match-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "match-v48.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const ownerFact = CaseTrace.evidenceFact({ source: "mcp", category: "repo_fact", summary: "owner", data: { subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `const verificationFact = CaseTrace.evidenceFact({ source: "tool", category: "verification", summary: "tests passed", data: { command: "node test/pricing.test.mjs", exit_code: 0, stdout: "pricing tests passed" } })`,
        `CaseTrace.responseOutput({ text: "Owner is billing-platform.", source_refs: ownerFact && verificationFact ? ["evidence:" + ownerFact.node_id, "evidence:" + verificationFact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "match-v48-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "match-v48-case", "trace.json"), "utf8")) as any
    const verification = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "verification_output",
    )
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")

    expect(claim.data.matched_evidence_refs).not.toContain(`evidence:${verification.record_id}`)
    expect(claim.data.match_reasons).toContain("owner_value")
    expect(claim.data.quality_flags).not.toContain("weak_evidence_match")
  })

  test("prefers verification evidence for test-result claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v49-verification-pref-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "verification-pref-v49.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const codeFact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "discount cap", data: { path: "src/pricing.mjs", line_start: 6, line_end: 6, text: "const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)" } })`,
        `const verificationFact = CaseTrace.evidenceFact({ source: "tool", category: "verification", summary: "tests passed", data: { command: "npm test", exit_code: 0, stdout: "pricing tests passed" } })`,
        `CaseTrace.responseOutput({ text: "修复后 npm test 全部通过。", source_refs: codeFact && verificationFact ? ["evidence:" + codeFact.node_id, "evidence:" + verificationFact.node_id, "verification:ver_passed"] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "verification-pref-v49-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "verification-pref-v49-case", "trace.json"), "utf8"),
    ) as any
    const codeFact = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "code_reference",
    )
    const verificationFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "verification_output",
    )
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")

    expect(claim.data.direct_evidence_refs).toContain(`evidence:${verificationFact.record_id}`)
    expect(claim.data.direct_evidence_refs).not.toContain(`evidence:${codeFact.record_id}`)
    expect(claim.data.execution_refs).toContain("verification:ver_passed")
    expect(claim.data.match_reasons).toContain("verification_result")
  })

  test("matches genericity check numeric result claims to verification evidence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v60-genericity-match-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "genericity-match-v60.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const verificationFact = CaseTrace.evidenceFact({ source: "tool", category: "verification", summary: "Extra genericity check", data: { command: "node -e renewalQuote", exit_code: 0, output: "extra check result: 850000\\\\nextra check passed: true\\\\n" } })`,
        `CaseTrace.responseOutput({ text: "\`renewalQuote({ baseCents: 10000, seats: 100, loyaltyYears: 10 })\` = **850000** ✅", source_refs: verificationFact ? ["evidence:" + verificationFact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "genericity-match-v60-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "genericity-match-v60-case", "trace.json"), "utf8"),
    ) as any
    const verification = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "verification_output",
    )
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")

    expect(claim.data.direct_evidence_refs).toContain(`evidence:${verification.record_id}`)
    expect(claim.data.quality_flags).not.toContain("context_only_claim")
    expect(claim.data.match_reasons).toContain("verification_result")
  })

  test("writes v6.0 structurally safe trace JSON and classifies finalized lifecycle records", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v45-quality-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "quality-v45.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "quality trace" }, environment: { model: "unit-test" } })`,
        `const shared = { path: "src/pricing.mjs", line_start: 1, line_end: 12 }`,
        `const fact = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "pricing file observed", data: { path: "src/pricing.mjs", line_start: 1, line_end: 12, symbol: "renewalQuote" }, source_locations: [shared] })`,
        `CaseTrace.responseOutput({ text: "renewalQuote is in src/pricing.mjs.", source_refs: fact ? ["evidence:" + fact.node_id, "context_snapshot:ctx_manual", "tool_span:span_manual"] : [], source_locations: [shared] })`,
        `CaseTrace.llmTurn({ turn_id: "open_turn", session_id: "ses_quality", message_id: "msg_user", agent: "build", agent_role: "main", provider_id: "deepseek", model_id: "unit-test", status: "running" })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_quality", message_id: "msg_user", agent: "build", phase: "turn.started", status: "running", summary: "open lifecycle" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "quality-v45-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "quality-v45-case")
    const traceText = await fs.readFile(path.join(caseDir, "trace.json"), "utf8")
    const trace = JSON.parse(traceText) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")

    expect(trace.trace_version).toBe("6.0")
    expect(traceText).not.toContain("[Circular]")
    expect(trace.records.filter((record: any) => record.status === "running")).toHaveLength(0)
    const openTurn = trace.records.find((record: any) => record.record_id === "llmturn_open_turn")
    expect(openTurn.status).toBe("success")
    expect(openTurn.data.finalized_status).toBe("finalized_without_close")
    expect(openTurn.data.finalized_reason).toBe("trace_finished")
    const health = trace.metrics.trace_health
    expect(health.circular_reference_markers).toBe(0)
    expect(health.finalized_open_records).toBeGreaterThan(0)
    expect(health.expected_lifecycle_finalized_records).toBeGreaterThan(0)
    expect(health.unexpected_missing_close_records).toBe(0)
    expect(health.issues.some((issue: any) => issue.kind === "finalized_open_record")).toBe(false)
    expect(html).toContain('id="trace-health"')
  })

  test("filters low-value runtime events from formal provenance and normalizes legacy relations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-contract-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-contract.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "prompt", event_type: "prompt.parts.resolved", data: { part_count: 1, part_types: ["text"], tool_overrides: {} } })`,
        `CaseTrace.event({ component: "processor", event_type: "tool-input-delta", data: { id: "call_1", delta: " owns" } })`,
        `CaseTrace.edge({ from: { type: "span", id: "span_tool", label: "edit" }, to: { type: "change", id: "chg_1" }, relation: "tool_to_change", label: "legacy relation" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-contract-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "semantic-contract-case", "trace.json"), "utf8"),
    ) as any
    const eventTypes = provenance.records.map((record: any) => record.event_type)
    const relations = provenance.dataflow_edges.map((edge: any) => edge.relation)

    expect(eventTypes).not.toContain("runtime.event")
    expect(JSON.stringify(provenance.records)).not.toContain("tool-input-delta")
    expect(JSON.stringify(provenance.records)).not.toContain("prompt.parts.resolved")
    expect(relations).toContain("modified_by")
    expect(relations).not.toContain("tool_to_change")
    expect(provenance.metrics.stream_summary.tool_input_delta_events).toBe(1)
    expect(provenance.metrics.stream_summary.prompt_parts_resolved_events).toBe(1)
    expect(provenance.metrics.trace_health.raw_stream_delta_events).toBe(1)
  })

  test("adds v6.0 semantic fields for source locations, compaction ledger, response visibility, and honest subagent trace refs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v4-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-fields.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const task = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "inspect pricing" } })`,
        `task?.end({ output: { task_id: "ses_child_1", output: "pricing owns discount math " + "x".repeat(5000) } })`,
        `const compaction = CaseTrace.compaction({ trigger: "auto", input_tokens: 24000, context_limit: 12000, selected_head_messages: 1, selected_tail_messages: 2, hidden_compaction_messages: 3, output_summary: "kept pricing fact" })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "read pricing", data: { path: "src/pricing.mjs", line_start: 10, line_end: 14, snippet: "return price * (1 - percent)" }, source_refs: compaction ? ["compaction:" + compaction.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "Intermediate task note.", response_role: "intermediate_summary" })`,
        `CaseTrace.responseOutput({ text: "Pricing owns discount math.", source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-fields-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "semantic-fields-case", "trace.json"), "utf8"),
    ) as any
    const compaction = provenance.records.find((record: any) => record.event_type === "context.compaction")
    const observation = provenance.records.find((record: any) => record.event_type === "execution.observation")
    const responses = provenance.records.filter((record: any) => record.event_type === "response.output")
    const response = responses.find((record: any) => record.data.response_role === "final_answer")
    const intermediate = responses.find((record: any) => record.data.response_role === "intermediate_summary")
    const subagent = provenance.records.find((record: any) => record.event_type === "subagent.call")

    expect(compaction.data.context_ledger.algorithm).toBe("head-tail-summary")
    expect(compaction.data.context_ledger.ledger_id_quality).toBe("estimated")
    expect(compaction.data.context_ledger.retained_message_ids).toHaveLength(3)
    expect(compaction.data.context_ledger.dropped_message_ids).toHaveLength(3)
    expect(observation.source_locations[0]).toMatchObject({ path: "src/pricing.mjs", line_start: 10, line_end: 14 })
    expect(response.data.visibility).toBe("user_visible")
    expect(response.data.is_final_for_case).toBe(true)
    expect(intermediate.data.is_final_for_case).toBe(false)
    expect(subagent.data.child_session_id).toBe("ses_child_1")
    expect(subagent.data.child_trace_available).toBe(false)
    expect(subagent.data.child_trace_dir).toBeUndefined()
    expect(subagent.data.trace_ref.child_trace_dir).toBeUndefined()
    expect(subagent.data.child_status).toBe("success")
    expect(subagent.data.output_artifact_id).toBeTruthy()
  })

  test("promotes MCP JSON text facts onto mcp.call records as well as observations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-mcp-call-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "mcp-call-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const fact = {
      key: "pricing-owner",
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
      fact: "pricing.mjs owns coupon math and promotion stacking.",
    }

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "mcp", operation: "tool.call", name: "trace-facts:audit_facts", input: { server: "trace-facts", tool: "audit_facts", args: { topic: "pricing" } } })`,
        `const output = { server: "trace-facts", tool: "audit_facts", content: [{ type: "text", text: ${JSON.stringify(JSON.stringify(fact))} }] }`,
        `span?.end({ output })`,
        `CaseTrace.observation({ source: "mcp", category: "trace-facts:audit_facts", summary: "pricing owner", data: output, source_refs: span ? ["span:" + span.id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "mcp-call-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "mcp-call-facts-case", "trace.json"), "utf8")) as any
    const mcpCall = trace.records.find((record: any) => record.event_type === "mcp.call")
    const observation = trace.records.find((record: any) => record.event_type === "execution.observation")

    expect(mcpCall.typed_resources[0]).toMatchObject({
      type: "repo_fact",
      key: "pricing-owner",
      fact: "pricing.mjs owns coupon math and promotion stacking.",
    })
    expect(mcpCall.source_locations[0]).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
    })
    expect(observation.typed_resources[0]).toMatchObject(mcpCall.typed_resources[0])
    expect(mcpCall.data.output_preview).toContain("pricing.mjs owns coupon math")
    expect(mcpCall.data.consumed_by_refs).toContain(`observation:${observation.record_id}`)
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.id === mcpCall.record_id && edge.to.id === observation.record_id && edge.relation === "returned_by",
      ),
    ).toBe(true)
  })

  test("records loop decisions as formal facts when processor loop events are observed", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-loop-decision-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "loop-decision.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "already answered" })`,
        `CaseTrace.event({ component: "processor", event_type: "step.finish", data: { agent: "build", messageID: "msg_1", reason: "stop", part_count: 2, part_types: ["text"], synthetic_continue: false, compaction_continue: false } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "loop-decision-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "loop-decision-case", "trace.json"), "utf8")) as any
    const loopDecision = trace.records.find((record: any) => record.event_type === "loop.decision")

    expect(loopDecision).toBeTruthy()
    expect(loopDecision.data).toMatchObject({
      decision: "stop",
      reason: "stop",
      agent: "build",
      message_id: "msg_1",
      part_count: 2,
      part_types: ["text"],
      has_user_visible_response: true,
      has_final_answer: true,
      synthetic_continue: false,
      compaction_continue: false,
    })
  })

  test("promotes MCP JSON text facts into typed resources and source locations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-mcp-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "mcp-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const output = { server: "audit_facts", tool: "repo_fact", content: [{ type: "text", text: JSON.stringify({ subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 }) }] }`,
        `const obs = CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: "pricing owner", data: output })`,
        `const fact = CaseTrace.evidenceFact({ source: "mcp", category: "repo_fact", summary: "pricing owner", data: output, source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "renewalQuote is owned by billing-platform.", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "mcp-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "mcp-facts-case", "trace.json"), "utf8")) as any
    const observation = trace.records.find((record: any) => record.event_type === "execution.observation")

    expect(observation.typed_resources[0]).toMatchObject({
      type: "repo_fact",
      subject: "renewalQuote",
      predicate: "owner",
      value: "billing-platform",
    })
    expect(observation.typed_resources[0].source_location).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 10,
      line_end: 12,
    })
    expect(observation.source_locations[0]).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 10,
      line_end: 12,
    })
    const evidenceFact = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")
    expect(evidenceFact.data.structured_claim).toMatchObject({
      subject: "renewalQuote",
      predicate: "owner",
      value: "billing-platform",
      extraction_method: "mcp_json_text",
      source_span: {
        path: "src/pricing.mjs",
        line_start: 10,
        line_end: 12,
      },
    })
    expect(evidenceFact.data.quality_flags).toContain("mcp_json_fact_extracted")
    expect(evidenceFact.data.quality_flags).not.toContain("generic_mcp_fact")
    const responseClaim = trace.records.find((record: any) => record.event_type === "response.claim")
    expect(responseClaim.data.matched_evidence_refs).toContain(`evidence:${evidenceFact.record_id}`)
    expect(responseClaim.data.support_level).toBe("direct")
    const html = await fs.readFile(path.join(dir, "mcp-facts-case", "trace.html"), "utf8")
    expect(html).toContain("Typed Resources")
    expect(html).toContain("repo_fact")
  })

  test("extracts line-level semantic facts from file read payloads before path-only fallback", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-line-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "line-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "architecture file observed", data: { path: "docs/architecture.md", output: "<path>docs/architecture.md</path>\\n<content>\\n9: The total discount must be capped at 15 percent for renewalQuote requests.\\n</content>" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "line-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "line-facts-case", "trace.json"), "utf8")) as any
    const evidenceFact = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")

    expect(evidenceFact.data.structured_claim).toMatchObject({
      subject: "renewalQuote",
      predicate: "discount_cap",
      value: "15 percent",
      extraction_method: "source_line_pattern",
      source_span: {
        path: "docs/architecture.md",
        line_start: 9,
        line_end: 9,
      },
    })
    expect(evidenceFact.data.quality_flags).toContain("line_fact_extracted")
    expect(evidenceFact.data.quality_flags).not.toContain("path_only_evidence_fact")
    expect(trace.metrics.trace_health.path_only_evidence_facts).toBe(0)
  })

  test("suppresses weak path-only tool output observations from formal provenance", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-weak-observation-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "weak-observation.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "private/tmp/project/src/pricing.mjs", data: { path: "/private/tmp/project/src/pricing.mjs" } })`,
        `CaseTrace.observation({ source: "tool", category: "file_read", summary: "pricing formula", data: { path: "src/pricing.mjs", line_start: 7, line_end: 7, snippet: "return subtotal * percent" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "weak-observation-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "weak-observation-case", "trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(path.join(dir, "weak-observation-case"))
    const observations = trace.records.filter((record: any) => record.event_type === "execution.observation")

    expect(observations).toHaveLength(1)
    expect(observations[0].title).toBe("file_read")
    expect(JSON.stringify(trace.records)).not.toContain("private/tmp/project/src/pricing.mjs")
    expect(journal.every((entry: any) => typeof entry.operation === "string")).toBe(true)
    expect(journal.map((entry: any) => entry.sequence)).toEqual(journal.map((_: any, index: number) => index + 1))
    expect(journal.some((entry: any) => entry.data?.kind === "weak_observation_suppressed")).toBe(true)
  })

  test("converges tool span and lifecycle events into one canonical tool record", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-canonical-tool-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "canonical-tool.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "read", input: { callID: "call_canonical", tool: "read", args: { path: "src/owner.mjs" } } })`,
        `span?.event({ event_type: "tool.call", data: { callID: "call_canonical", tool: "read", args: { path: "src/owner.mjs" } } })`,
        `span?.event({ event_type: "tool.result", data: { callID: "call_canonical", tool: "read", args: { path: "src/owner.mjs" }, output: "export const owner = 'billing-platform'" } })`,
        `span?.end({ output: { content: "export const owner = 'billing-platform'" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "canonical-tool-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "canonical-tool-case", "trace.json"), "utf8")) as any
    const calls = trace.records.filter(
      (record: any) =>
        record.event_type === "tool.call" &&
        (record.data.call_id === "call_canonical" || record.data.input?.callID === "call_canonical"),
    )

    expect(calls).toHaveLength(1)
    expect(calls[0].status).toBe("success")
    expect(calls[0].data.request_status).toBe("completed")
    expect(calls[0].data.output).toBeDefined()
  })

  test("keeps path-only directory listings out of semantic facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-path-listing-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "path-listing.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "tool", category: "directory_listing", summary: "src/owner.mjs\\nsrc/pricing.mjs", data: { output: "src/owner.mjs\\nsrc/pricing.mjs" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "path-listing-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "path-listing-case", "trace.json"), "utf8")) as any
    expect(trace.records.some((record: any) => record.event_type === "evidence.semantic_fact")).toBe(false)
    expect(trace.records.some((record: any) => record.event_type === "execution.observation")).toBe(true)
  })

  test("aggregates repeated no-op compaction checks", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-compaction-check-aggregate-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-check-aggregate.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compactionCheck({ session_id: "ses_aggregate", model_id: "deepseek-v4-pro", token_estimate: 100, context_limit: 1000, overflow: false, selected_algorithm: "head-tail-summary" })`,
        `CaseTrace.compactionCheck({ session_id: "ses_aggregate", model_id: "deepseek-v4-pro", token_estimate: 140, context_limit: 1000, overflow: false, selected_algorithm: "head-tail-summary" })`,
        `CaseTrace.compactionCheck({ session_id: "ses_aggregate", model_id: "deepseek-v4-pro", token_estimate: 180, context_limit: 1000, overflow: false, selected_algorithm: "head-tail-summary" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-check-aggregate-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-check-aggregate-case", "trace.json"), "utf8"),
    ) as any
    const checks = trace.records.filter((record: any) => record.event_type === "context.compaction_check")

    expect(checks).toHaveLength(1)
    expect(checks[0].data.check_count).toBe(3)
    expect(checks[0].data.first_token_estimate).toBe(100)
    expect(checks[0].data.token_estimate).toBe(180)
  })

  test("downgrades earlier default user-visible responses to intermediate summaries", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-response-roles-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "response-roles.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "subagent-style preliminary answer" })`,
        `CaseTrace.responseOutput({ text: "final answer" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "response-roles-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "response-roles-case", "trace.json"), "utf8")) as any
    const responses = trace.records.filter((record: any) => record.event_type === "response.output")

    expect(responses[0].data.response_role).toBe("intermediate_summary")
    expect(responses[0].data.is_final_for_case).toBe(false)
    expect(responses[1].data.response_role).toBe("final_answer")
    expect(responses[1].data.is_final_for_case).toBe(true)
  })

  test("deduplicates artifact-backed provenance payloads by hash", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-dedupe-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-dedupe.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const repeated = "same-large-observation:" + "x".repeat(6000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: ${JSON.stringify(repeated)}, data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: ${JSON.stringify(repeated)}, data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-dedupe-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "causal-dedupe-case", "provenance-trace.json"), "utf8"),
    ) as any
    const samePayloadArtifacts = provenance.artifacts.filter((artifact: any) => artifact.label === "observation.data")

    expect(samePayloadArtifacts).toHaveLength(1)
    expect(samePayloadArtifacts[0].occurrences).toBe(3)
    expect(samePayloadArtifacts[0].path).toMatch(/^artifacts\/sha256\//)
    expect(samePayloadArtifacts[0].storage_encoding).toBe("json_minified")
    const stored = await fs.readFile(path.join(dir, "causal-dedupe-case", samePayloadArtifacts[0].path), "utf8")
    expect(() => JSON.parse(stored)).not.toThrow()
    expect(stored).not.toContain("\n  ")
    expect(samePayloadArtifacts[0].stored_length).toBe(Buffer.byteLength(stored))
    expect(samePayloadArtifacts[0].original_length).toBeGreaterThanOrEqual(samePayloadArtifacts[0].stored_length)
  })

  test("finalizes canonical partial and trace when a traced process receives SIGINT", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-sigint-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-sigint.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "long running" } })`,
        `CaseTrace.setSessionID("ses_sigint")`,
        `CaseTrace.agentLifecycle({ session_id: "ses_sigint", message_id: "msg_sigint", agent: "build", phase: "turn.started", status: "running", summary: "turn is open when SIGINT arrives" })`,
        `CaseTrace.node({ node_id: "fixture_signal_sigint_ready", kind: "verification", component: "runtime", title: "SIGINT fixture readiness marker" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-sigint-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const caseDir = path.join(dir, "causal-sigint-case")
    const persistedJournal = await waitForCompleteCausalIRCheckpoint(caseDir, "fixture_signal_sigint_ready")
    expect(persistedJournal).toBeDefined()
    assertCausalIRJournalAudit(persistedJournal!)
    proc.kill("SIGINT")
    await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(await waitForExists(path.join(caseDir, "manifest.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "provenance-trace.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "trace.html"))).toBe(true)
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(caseDir, "provenance-trace.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)

    expect(manifest.status).toBe("cancelled")
    expect(manifest.result.reason).toBe("SIGINT")
    expect(provenance.manifest.status).toBe("cancelled")
    assertFinalCancelledPartialMatchesTrace(partial, trace)
    assertJournalReplaysCanonicalTrace(journal, trace)
    assertFinalForcedCheckpointMatchesCanonicalTrace(journal, partial, trace)
  })

  test("records observation and compaction facts for offline provenance analysis", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-semantics.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const previousSummary = "Previous summary with pricing facts " + "x".repeat(3000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const compaction = CaseTrace.compaction({ trigger: "overflow", provider_id: "deepseek", model_id: "unit-test", input_tokens: 21000, context_limit: 20000, selected_head_messages: 8, selected_tail_messages: 2, hidden_compaction_messages: 1, previous_summary: ${JSON.stringify(previousSummary)}, serialized_tail: "tail message " + "y".repeat(3000), output_summary: "pricing facts preserved " + "z".repeat(3000), auto_continue: true, source_refs: ["context:ctx_before"], context_ledger: { algorithm: "head-tail-summary" } })`,
        `const obs = CaseTrace.observation({ source: "compaction", category: "preserved_fact", summary: "pricing fact preserved after compaction", data: { fact: "pricing owns discounts" }, source_refs: compaction ? ["compaction:" + compaction.node_id] : [] })`,
        `const fact = CaseTrace.evidenceFact({ source: "compaction", category: "preserved_fact", summary: "pricing fact preserved after compaction", data: { fact: "pricing owns discounts" }, source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "Pricing owns discounts.", source_refs: fact ? ["evidence:" + fact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-semantics-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "96",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "causal-semantics-case", "provenance-trace.json"), "utf8"),
    ) as any
    const compaction = provenance.records.find((record: any) => record.event_type === "context.compaction")
    const observation = provenance.records.find((record: any) => record.event_type === "execution.observation")

    expect(compaction).toBeTruthy()
    expect(compaction.data.trigger).toBe("overflow")
    expect(compaction.data.auto_continue).toBe(true)
    expect(compaction.data.previous_summary.artifact_id).toBeTruthy()
    expect(compaction.data.algorithm).toBe("head-tail-summary")
    expect(compaction.data.before_context_refs).toContain("context:ctx_before")
    expect(compaction.data.serialized_tail_artifact_ref).toBeTruthy()
    expect(compaction.data.summary_artifact_ref).toBeTruthy()
    expect(observation.data.source).toBe("compaction")
    expect(observation.source_refs).toContain(`compaction:${compaction.record_id}`)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "derived_from")).toBe(true)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "supported_response")).toBe(true)
  })

  test("promotes compaction ledger provenance into shallow attribution fields", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v49-compaction-fields-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-fields-v49.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compaction({ trigger: "auto", provider_id: "deepseek", model_id: "unit-test", input_tokens: 1200, context_limit: 1000, selected_head_messages: 1, selected_tail_messages: 2, input_message_count: 12, compaction_request_message_count: 4, output_message_count: 3, serialized_tail: "tail " + "x".repeat(1000), output_summary: "summary " + "y".repeat(1000), auto_continue: true, result: "continue", source_refs: ["context:ctx_before"], context_ledger: { algorithm: "head-tail-summary", algorithm_version: "head-tail-summary/v1", token_estimate_before: 1200, token_estimate_after: 300, retained_message_ids: ["msg_head", "msg_tail"], dropped_message_ids: ["msg_old"], retained_fact_refs: ["evidence:fact_keep"], dropped_fact_refs: ["evidence:fact_drop"], auto_continue_prompt_ref: "message:msg_continue" }, metadata: { after_context_refs: ["context:ctx_after"] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-fields-v49-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "96",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-fields-v49-case", "trace.json"), "utf8"),
    ) as any
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(compaction.data.algorithm).toBe("head-tail-summary")
    expect(compaction.data.algorithm_version).toBe("head-tail-summary/v1")
    expect(compaction.data.input_message_count).toBe(12)
    expect(compaction.data.compaction_request_message_count).toBe(4)
    expect(compaction.data.output_message_count).toBe(3)
    expect(compaction.data.before_context_refs).toEqual(["context:ctx_before"])
    expect(compaction.data.after_context_refs).toEqual(["context:ctx_after"])
    expect(compaction.data.serialized_tail_artifact_ref).toBeTruthy()
    expect(compaction.data.summary_artifact_ref).toBeTruthy()
    expect(compaction.data.retained_message_ids).toEqual(["msg_head", "msg_tail"])
    expect(compaction.data.dropped_message_ids).toEqual(["msg_old"])
    expect(compaction.data.retained_fact_refs).toEqual(["evidence:fact_keep"])
    expect(compaction.data.dropped_fact_refs).toEqual(["evidence:fact_drop"])
    expect(compaction.data.token_estimate_before).toBe(1200)
    expect(compaction.data.token_estimate_after).toBe(300)
    expect(compaction.data.auto_continue_prompt_ref).toBe("message:msg_continue")
  })

  test("keeps small compaction summaries as artifacts and derives auto-continue after refs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v50-compaction-artifact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-artifact-v50.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compaction({ trigger: "auto", provider_id: "deepseek", model_id: "unit-test", output_summary: "short preserved summary", serialized_tail: "short tail", auto_continue: true, source_refs: ["context:ctx_before"], context_ledger: { algorithm: "head-tail-summary", auto_continue_prompt_ref: "message:msg_continue" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-artifact-v50-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-artifact-v50-case", "trace.json"), "utf8"),
    ) as any
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(compaction.data.summary_artifact_ref).toBeTruthy()
    expect(compaction.data.after_context_refs).toContain("message:msg_continue")
    expect(compaction.data.context_ledger.quality_flags).not.toContain("summary_artifact_pending")
    expect(trace.artifacts.some((artifact: any) => artifact.artifact_id === compaction.data.summary_artifact_ref)).toBe(
      true,
    )
  })

  test("backfills forced compaction estimates from nearest compaction check", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v51-compaction-estimate-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "compaction-estimate-v51.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compactionCheck({ session_id: "ses_compact", message_id: "msg_user", provider_id: "deepseek", model_id: "unit-test", token_estimate: 4096, context_limit: 8192, reserved_tokens: 1024, overflow: true, selected_algorithm: "head-tail-summary", trigger_reason: "forced_test" })`,
        `CaseTrace.compaction({ trigger: "auto", session_id: "ses_compact", message_id: "msg_user", provider_id: "deepseek", model_id: "unit-test", output_summary: "summary", serialized_tail: "tail", auto_continue: true, context_ledger: { algorithm: "head-tail-summary", auto_continue_prompt_ref: "message:msg_continue" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "compaction-estimate-v51-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "compaction-estimate-v51-case", "trace.json"), "utf8"),
    ) as any
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(compaction.data.token_estimate_before).toBe(4096)
    expect(compaction.data.estimate_source).toBe("nearest_compaction_check")
    expect(compaction.data.context_ledger.quality_flags).not.toContain("missing_token_estimate")
    expect(trace.metrics.trace_health.compaction_quality_flags.missing_token_estimate).toBeUndefined()
  })

  test("adds payload refs and dedupe group ids to large field summaries", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v51-payload-ref-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "payload-ref-v51.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const large = "x".repeat(512)`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_payload", input: { payload: large }, output: { payload: large } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "payload-ref-v51-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "payload-ref-v51-case", "trace.json"), "utf8")) as any
    const transform = trace.records.find((record: any) => record.event_type === "context.transform")

    expect(transform.data.input.payload_ref).toBeTruthy()
    expect(transform.data.input.payload_dedupe_group_id).toBeTruthy()
    expect(transform.data.output.payload_ref).toBeTruthy()
    expect(transform.data.output.payload_dedupe_group_id).toBeTruthy()
  })

  test("records why subagent child trace is unavailable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v50-subagent-reason-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-reason-v50.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "summarize docs" } })`,
        `span?.end({ output: { child_session_id: "ses_missing_child", child_status: "success", output: "done" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-reason-v50-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "subagent-reason-v50-case", "trace.json"), "utf8")) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")

    expect(subagent.data.child_trace_available).toBe(false)
    expect(subagent.data.child_trace_unavailable_reason).toBe("child_trace_file_not_found")
  })

  test("marks child subagent records as inline when they are present in the same trace", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v51-inline-subagent-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "inline-subagent-v51.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "summarize docs" } })`,
        `span?.end({ output: { child_session_id: "ses_child_inline", child_status: "success", output: "child result" } })`,
        `CaseTrace.promptAssembly({ stage: "subagent_prompt", session_id: "ses_child_inline", agent: "general", input: { prompt: "summarize large requirements" }, output: { message_id: "msg_child_user" } })`,
        `CaseTrace.observation({ source: "tool", category: "file_read", summary: "child read large requirements", data: { session_id: "ses_child_inline", path: "docs/large-requirements.md" } })`,
        `CaseTrace.responseOutput({ response_role: "subagent_result", text: "Child result: owner is billing-platform.", metadata: { session_id: "ses_child_inline", message_id: "msg_child_assistant" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "inline-subagent-v51-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "inline-subagent-v51-case", "trace.json"), "utf8")) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")
    const html = await fs.readFile(path.join(dir, "inline-subagent-v51-case", "trace.html"), "utf8")

    expect(subagent.data.child_trace_available).toBe(true)
    expect(subagent.data.child_trace_mode).toBe("inline_same_trace")
    expect(subagent.data.child_record_count).toBeGreaterThan(0)
    expect(subagent.data.child_record_refs.length).toBeGreaterThan(0)
    expect(subagent.data.child_prompt_refs.length).toBeGreaterThan(0)
    expect(subagent.data.child_result_refs.length).toBeGreaterThan(0)
    expect(subagent.data.child_trace_unavailable_reason).toBeUndefined()
    expect(html).toContain("inline_same_trace")
  })

  test("links parent consumption when parent response references child evidence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-subagent-evidence-consumed-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-evidence-consumed-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "find owner" } })`,
        `span?.end({ output: { child_session_id: "ses_child_evidence", child_status: "success", output: "owner is billing-platform" } })`,
        `const childFact = CaseTrace.evidenceFact({ source: "mcp", category: "repo_fact", summary: "owner fact from child", data: { session_id: "ses_child_evidence", subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `CaseTrace.responseOutput({ response_role: "subagent_result", text: "Child result: owner is billing-platform.", metadata: { session_id: "ses_child_evidence" } })`,
        `CaseTrace.responseOutput({ text: "The parent answer uses the child evidence: renewalQuote owner is billing-platform.", source_refs: childFact ? ["evidence:" + childFact.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-evidence-consumed-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "subagent-evidence-consumed-v56-case", "trace.json"), "utf8"),
    ) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")

    expect(subagent.data.child_key_evidence_refs.length).toBeGreaterThan(0)
    expect(subagent.data.parent_consumption_refs.length).toBeGreaterThan(0)
    expect(subagent.data.parent_consumption_refs.some((ref: string) => ref.startsWith("response_segment:"))).toBe(true)
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.id === subagent.record_id && edge.to.type === "response_segment" && edge.relation === "reported_to",
      ),
    ).toBe(true)
  })

  test("links parent consumption by exact child-result content without altering agent input", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v63-subagent-content-consumed-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-content-consumed-v63.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "find owner" } })`,
        `span?.end({ output: { child_session_id: "ses_child_content", child_status: "success", output: "renewalQuote owner is billing-platform" } })`,
        `CaseTrace.aliasSession("ses_child_content", "ses_parent")`,
        `CaseTrace.responseOutput({ response_role: "subagent_result", text: "renewalQuote owner is billing-platform", metadata: { session_id: "ses_child_content" } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_parent", message_id: "msg_parent", input: { messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] }, output: { model_messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] } })`,
        `CaseTrace.responseOutput({ text: "The renewalQuote owner is billing-platform." })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-content-consumed-v63-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "subagent-content-consumed-v63-case", "trace.json"), "utf8"),
    ) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")
    const parentContext = trace.records.find(
      (record: any) => record.event_type === "context.transform" && record.data.session_id === "ses_parent",
    )

    expect(subagent.data.parent_consumption_refs).toContain(`context:${parentContext.record_id}`)
    expect(subagent.data.parent_consumption_evidence).toContainEqual(
      expect.objectContaining({
        consumer_ref: `context:${parentContext.record_id}`,
        evidence_tier: "content_matched",
        behavior_impact: "none",
      }),
    )
  })

  test("does not infer parent consumption from an internal child tool result", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-subagent-internal-result-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-internal-result.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "find owner" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", data: { sessionID: "ses_child_internal", callID: "call_child_read", tool: "read", output: "shared internal code fragment" } })`,
        `span?.end({ output: { child_session_id: "ses_child_internal", child_status: "success", output: "final owner is billing-platform" } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_parent", message_id: "msg_parent", input: { messages: [{ role: "user", content: "shared internal code fragment" }] }, output: { model_messages: [{ role: "user", content: "shared internal code fragment" }] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-internal-result-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "subagent-internal-result-case", "trace.json"), "utf8"),
    ) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")

    expect(subagent.data.parent_consumption_refs).toEqual([])
    expect(subagent.data.parent_consumption_evidence).toEqual([])
  })

  test("matches subagent consumption only after child completion and inside the parent session", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-subagent-parent-boundary-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "subagent-parent-boundary.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "delegate" }, environment: { model: "unit-test" } })`,
        `CaseTrace.aliasSession("ses_child_boundary", "ses_parent")`,
        `CaseTrace.contextTransform({ stage: "before_child", session_id: "ses_parent", message_id: "msg_before", input: { messages: [{ role: "user", content: "renewalQuote owner is billing-platform" }] }, output: { model_messages: [{ role: "user", content: "renewalQuote owner is billing-platform" }] } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "find owner", parent_session_id: "ses_parent", message_id: "msg_parent" } })`,
        `span?.end({ output: { child_session_id: "ses_child_boundary", child_status: "success", output: "renewalQuote owner is billing-platform" } })`,
        `CaseTrace.responseOutput({ response_role: "subagent_result", text: "renewalQuote owner is billing-platform", metadata: { session_id: "ses_child_boundary", message_id: "msg_child" } })`,
        `CaseTrace.contextTransform({ stage: "other_parent", session_id: "ses_other", message_id: "msg_other", input: { messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] }, output: { model_messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] } })`,
        `CaseTrace.contextTransform({ stage: "after_child", session_id: "ses_parent", message_id: "msg_after", input: { messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] }, output: { model_messages: [{ role: "tool", content: "renewalQuote owner is billing-platform" }] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "subagent-parent-boundary-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "subagent-parent-boundary-case", "trace.json"), "utf8"),
    ) as any
    const otherTrace = JSON.parse(
      await fs.readFile(
        path.join(dir, routedCaseDirectoryForTest("subagent-parent-boundary-case", "root", "ses_other"), "trace.json"),
        "utf8",
      ),
    ) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")
    const consumerStages = subagent.data.parent_consumption_refs
      .map((ref: string) => ref.replace(/^context:/, ""))
      .map((id: string) => trace.records.find((record: any) => record.record_id === id)?.data?.stage)
      .filter(Boolean)

    expect(consumerStages).toEqual(["after_child"])
    expect(
      otherTrace.records.some(
        (record: any) => record.event_type === "context.transform" && record.data.stage === "other_parent",
      ),
    ).toBe(true)
  })

  test("treats run and lifecycle records cancelled at trace finish as expected finalization", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v51-lifecycle-cancelled-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "lifecycle-cancelled-v51.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "server case" }, environment: { model: "unit-test" } })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_cancel", message_id: "msg_user", agent: "build", phase: "turn.started", status: "running", summary: "server turn still open when process exits" })`,
        `CaseTrace.finish({ status: "cancelled" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "lifecycle-cancelled-v51-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "lifecycle-cancelled-v51-case", "trace.json"), "utf8"),
    ) as any
    const health = trace.metrics.trace_health

    expect(health.finalized_open_records).toBeGreaterThan(0)
    expect(health.expected_lifecycle_finalized_records).toBeGreaterThan(0)
    expect(health.unexpected_missing_close_records).toBe(0)
    expect(health.issues.some((issue: any) => issue.kind === "unexpected_missing_close_record")).toBe(false)
  })

  test("marks open lifecycle records as closed after completed case shutdown", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v53-case-status-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "case-status-v53.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "server case" }, environment: { model: "unit-test" } })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_case", message_id: "msg_user", agent: "build", phase: "turn.started", status: "running", summary: "open http turn" })`,
        `CaseTrace.llmTurn({ turn_id: "post_final_turn", session_id: "ses_case", message_id: "msg_user", agent: "build", agent_role: "main", provider_id: "deepseek", model_id: "unit-test", status: "running" })`,
        `CaseTrace.responseOutput({ text: "Final answer completed over HTTP.", source_refs: ["execution:request_1"], metadata: { response_role: "final_answer", visibility: "user_visible", is_final_for_case: true } })`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGINT" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "case-status-v53-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "case-status-v53-case", "trace.json"), "utf8")) as any
    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "case-status-v53-case", "manifest.json"), "utf8"),
    ) as any
    const caseRecord = trace.records.find((record: any) => record.event_type === "case.completed")
    const finalizedLifecycle = trace.records.find(
      (record: any) => record.event_type === "agent.lifecycle" && record.data?.phase === "turn.started",
    )
    const finalizedLlmTurn = trace.records.find((record: any) => record.record_id === "llmturn_post_final_turn")

    expect(trace.trace_version).toBe("6.0")
    expect(manifest.status).toBe("success")
    expect(manifest.server_status).toBe("cancelled")
    expect(manifest.process_status).toBe("cancelled")
    expect(manifest.case_status).toBe("success")
    expect(caseRecord).toBeTruthy()
    expect(caseRecord.status).toBe("success")
    expect(caseRecord.data.server_status).toBe("cancelled")
    expect(caseRecord.data.case_status).toBe("success")
    expect(finalizedLifecycle.status).toBe("success")
    expect(finalizedLifecycle.data.finalized_status).toBe("closed_after_case_completion")
    expect(finalizedLifecycle.data.finalized_reason).toBe("service_shutdown_after_completion")
    expect(finalizedLlmTurn.status).toBe("success")
    expect(finalizedLlmTurn.data.finalized_status).toBe("closed_after_case_completion")
    expect(finalizedLlmTurn.data.finalized_reason).toBe("service_shutdown_after_completion")
    expect(trace.metrics.trace_health.cancelled_after_case_completion_records).toBe(0)
    expect(trace.metrics.trace_health.closed_after_case_completion_records).toBeGreaterThan(0)
    expect(trace.metrics.trace_health.llm_turns_missing_token_usage).toBe(0)
    expect(trace.metrics.trace_health.llm_turns_missing_finish_reason).toBe(0)
    expect(trace.metrics.trace_health.issues.map((issue: any) => issue.kind)).not.toContain(
      "closed_after_case_completion",
    )
    expect(trace.metrics.trace_health.issues.map((issue: any) => issue.kind)).not.toContain(
      "llm_turn_missing_token_usage",
    )
  })

  test("keeps process-signal shutdown separate from successful case status in manifest", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v59-case-process-status-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "case-process-status-v59.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "server completed over HTTP" }, environment: { model: "unit-test" } })`,
        `CaseTrace.responseOutput({ text: "Final answer completed over HTTP.", metadata: { response_role: "final_answer", visibility: "user_visible", is_final_for_case: true, finality_source: "explicit" } })`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGINT", signal: "SIGINT", trace_html_flush: "process_signal" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "case-process-status-v59-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "case-process-status-v59-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const caseRecord = trace.records.find((record: any) => record.event_type === "case.completed")

    expect(manifest.status).toBe("success")
    expect(manifest.case_status).toBe("success")
    expect(manifest.server_status).toBe("cancelled")
    expect(manifest.process_status).toBe("cancelled")
    expect(manifest.server_shutdown_reason).toBe("process_signal")
    expect(manifest.shutdown_signal).toBe("SIGINT")
    expect(manifest.shutdown_disposition).toBe("graceful_after_case_completion")
    expect(caseRecord.data.case_status).toBe("success")
    expect(caseRecord.data.server_status).toBe("cancelled")
    expect(caseRecord.data.process_status).toBe("cancelled")
    expect(caseRecord.data.server_shutdown_reason).toBe("process_signal")
    expect(caseRecord.data.shutdown_signal).toBe("SIGINT")
    expect(caseRecord.data.shutdown_disposition).toBe("graceful_after_case_completion")
  })

  test("does not infer case success from an implicit final response after cancellation", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v56-cancelled-finality-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "cancelled-finality-v56.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "server timeout case" }, environment: { model: "unit-test" } })`,
        `CaseTrace.responseOutput({ text: "Now let me read all relevant files before answering.", source_refs: ["tool_call:call_read"] })`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGTERM", signal: "SIGTERM" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "cancelled-finality-v56-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "cancelled-finality-v56-case", "trace.json"), "utf8"),
    ) as any
    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "cancelled-finality-v56-case", "manifest.json"), "utf8"),
    ) as any
    const caseRecord = trace.records.find((record: any) => record.event_type === "case.failed")
    const response = trace.records.find((record: any) => record.event_type === "response.output")

    expect(manifest.server_status).toBe("cancelled")
    expect(manifest.case_status).toBe("cancelled")
    expect(manifest.shutdown_signal).toBe("SIGTERM")
    expect(manifest.shutdown_disposition).toBe("interrupted_before_case_completion")
    expect(caseRecord).toBeTruthy()
    expect(caseRecord.data.case_status).toBe("cancelled")
    expect(caseRecord.data.shutdown_signal).toBe("SIGTERM")
    expect(caseRecord.data.shutdown_disposition).toBe("interrupted_before_case_completion")
    expect(response.data.metadata.finality_source).toBe("inferred")
    expect(trace.records.some((record: any) => record.event_type === "case.completed")).toBe(false)
  })

  test("finalizes canonical partial and trace when a process receives SIGTERM", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v58-signal-flush-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "signal-flush-v58.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "long running signal case" }, environment: { model: "unit-test" } })`,
        `CaseTrace.agentLifecycle({ session_id: "ses_signal", message_id: "msg_user", agent: "build", phase: "turn.started", status: "running", summary: "turn is open when SIGTERM arrives" })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "observed pricing file before signal", data: { path: "src/pricing.mjs", line_start: 1, line_end: 1, text: "export function renewalQuote(input) {}" } })`,
        `CaseTrace.node({ node_id: "fixture_signal_sigterm_canonical_ready", kind: "verification", component: "runtime", title: "SIGTERM canonical fixture readiness marker" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "signal-flush-v58-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const caseDir = path.join(dir, "signal-flush-v58-case")
    const persistedJournal = await waitForCompleteCausalIRCheckpoint(caseDir, "fixture_signal_sigterm_canonical_ready")
    expect(persistedJournal).toBeDefined()
    assertCausalIRJournalAudit(persistedJournal!)

    proc.kill("SIGTERM")
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(143)
    expect(await exists(path.join(caseDir, "trace.html"))).toBe(true)
    expect(await exists(path.join(caseDir, "trace.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const caseRecord = trace.records.find((record: any) => record.event_type === "case.failed")
    const journal = await readCausalIRJournal(caseDir)

    expect(manifest.server_status).toBe("cancelled")
    expect(manifest.case_status).toBe("cancelled")
    expect(caseRecord).toBeTruthy()
    expect(caseRecord.data.case_status).toBe("cancelled")
    expect(trace.records.some((record: any) => record.event_type === "evidence.semantic_fact")).toBe(true)
    expect(html).toContain("case cancelled")
    expect(html).toContain("observed pricing file before signal")
    assertFinalCancelledPartialMatchesTrace(partial, trace)
    assertJournalReplaysCanonicalTrace(journal, trace)
    assertFinalForcedCheckpointMatchesCanonicalTrace(journal, partial, trace)
  })

  test("trace publication reports an explicit finish once after terminal persistence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-publication-finish-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "trace-publication-finish.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.setSessionID("ses_publication")`,
        `CaseTrace.finish({ status: "success" })`,
        `CaseTrace.finish({ status: "success" })`,
        `process.stdout.write("agent output\\n")`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_QUIET: "0",
        OPENCODE_CASE_ID: "trace-publication-finish",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stdout = await new Response(proc.stdout).text()
    const stderr = await new Response(proc.stderr).text()
    const caseDir = path.join(dir, "trace-publication-finish")

    expect(code).toBe(0)
    expect(stdout).toBe("agent output\n")
    expect(stderr.match(/Session trace saved/g)).toHaveLength(1)
    expect(stderr).toContain("session: ses_publication")
    expect(stderr).toContain(path.join(caseDir, "trace.html"))
    expect(stderr).toContain(path.join(caseDir, "trace.json"))
    expect(await exists(path.join(caseDir, "trace.json"))).toBe(true)
  })

  test("trace publication reports SIGTERM persistence once without changing its exit code", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-publication-sigterm-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "trace-publication-sigterm.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.setSessionID("ses_publication")`,
        `CaseTrace.node({ node_id: "trace_publication_sigterm_ready", kind: "verification", component: "runtime", title: "ready" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_QUIET: "0",
        OPENCODE_CASE_ID: "trace-publication-sigterm",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const caseDir = path.join(dir, "trace-publication-sigterm")
    expect(await waitForCompleteCausalIRCheckpoint(caseDir, "trace_publication_sigterm_ready")).toBeDefined()

    proc.kill("SIGTERM")
    const code = await proc.exited
    const stdout = await new Response(proc.stdout).text()
    const stderr = await new Response(proc.stderr).text()

    expect(code).toBe(143)
    expect(stdout).toBe("")
    expect(stderr.match(/Session trace saved/g)).toHaveLength(1)
    expect(stderr).toContain("session: ses_publication")
    expect(stderr).toContain(path.join(caseDir, "trace.html"))
    expect(stderr).toContain(path.join(caseDir, "trace.json"))
    expect(await exists(path.join(caseDir, "trace.json"))).toBe(true)
  })

  const publicationExitFixtures: ReadonlyArray<{
    mode: string
    exitCode: number
    status: string
    signal?: NodeJS.Signals
    partialOnly?: boolean
  }> = [
    { mode: "beforeExit", exitCode: 0, status: "completed" },
    { mode: "SIGINT", exitCode: 130, status: "cancelled", signal: "SIGINT" },
    { mode: "SIGHUP", exitCode: 129, status: "cancelled", signal: "SIGHUP" },
    { mode: "uncaughtException", exitCode: 1, status: "failed" },
    { mode: "unhandledRejection", exitCode: 1, status: "failed" },
    { mode: "partialEmergency", exitCode: 0, status: "partial", partialOnly: true },
  ]
  for (const fixture of publicationExitFixtures) {
    test(`trace publication covers ${fixture.mode} exactly once on stderr`, async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), `opencode-trace-publication-${fixture.mode}-`))
      const packageDir = path.resolve(import.meta.dir, "../..")
      const script = path.join(dir, `trace-publication-${fixture.mode}.ts`)
      const caseID = `trace-publication-${fixture.mode}`
      const marker = `trace_publication_${fixture.mode}_ready`
      const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
      const terminalLines =
        fixture.mode === "beforeExit"
          ? []
          : fixture.mode === "uncaughtException"
            ? [`throw new Error("publication uncaught exception")`]
            : fixture.mode === "unhandledRejection"
              ? [`Promise.reject(new Error("publication unhandled rejection"))`, `setInterval(() => {}, 1000)`]
              : fixture.mode === "partialEmergency"
                ? [
                    `const trace = CaseTrace.get() as any`,
                    `const originalSafeWrite = trace.safeWrite.bind(trace)`,
                    `trace.safeWrite = (target: string, content: unknown) => target === trace.partialFile ? originalSafeWrite(target, content) : false`,
                    `CaseTrace.finish({ status: "error", result: { reason: "forced_partial_emergency" } })`,
                  ]
                : [`setInterval(() => {}, 1000)`]

      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `CaseTrace.setSessionID("ses_${fixture.mode}")`,
          `CaseTrace.node({ node_id: ${JSON.stringify(marker)}, kind: "verification", component: "runtime", title: "ready" })`,
          `;(CaseTrace.get() as any).writePartial(true)`,
          ...terminalLines,
        ].join("\n"),
      )

      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_TRACE_QUIET: "0",
          OPENCODE_CASE_ID: caseID,
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const caseDir = path.join(dir, caseID)
      if (fixture.signal) {
        expect(await waitForCompleteCausalIRCheckpoint(caseDir, marker)).toBeDefined()
        proc.kill(fixture.signal)
      }
      const code = await proc.exited
      const stdout = await new Response(proc.stdout).text()
      const stderr = await new Response(proc.stderr).text()
      const traceFile = path.join(caseDir, "trace.json")
      const htmlFile = path.join(caseDir, "trace.html")
      const partialFile = path.join(caseDir, "partial", "latest.json")

      expect(code).toBe(fixture.exitCode)
      expect(stdout).toBe("")
      expect(stderr.match(/Session trace saved/g)).toHaveLength(1)
      expect(stderr).toContain(`session: ses_${fixture.mode}`)
      expect(stderr).toContain(`status: ${fixture.status}`)
      expect(stderr).toContain(`directory: ${caseDir}`)
      expect(stderr).toContain(`partial: ${partialFile}`)
      expect(await exists(partialFile)).toBe(true)
      if (fixture.partialOnly) {
        expect(stderr).not.toContain(`html: ${htmlFile}`)
        expect(stderr).not.toContain(`json: ${traceFile}`)
        expect(await exists(htmlFile)).toBe(false)
        expect(await exists(traceFile)).toBe(false)
      } else {
        expect(stderr).toContain(`html: ${htmlFile}`)
        expect(stderr).toContain(`json: ${traceFile}`)
        expect(await exists(htmlFile)).toBe(true)
        expect(await exists(traceFile)).toBe(true)
      }
    })
  }

  test("flushes before an earlier server signal listener exits the process", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-signal-listener-order-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "signal-listener-order.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `process.once("SIGTERM", () => process.exit(143))`,
        `CaseTrace.configure({ input: { prompt: "server listener was registered first" } })`,
        `CaseTrace.node({ node_id: "signal_listener_order_ready", kind: "verification", component: "runtime", title: "ready" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "signal-listener-order-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const caseDir = path.join(dir, "signal-listener-order-case")
    expect(await waitForCompleteCausalIRCheckpoint(caseDir, "signal_listener_order_ready")).toBeDefined()

    proc.kill("SIGTERM")
    expect(await proc.exited).toBe(143)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await exists(path.join(caseDir, "trace.json"))).toBe(true)

    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    expect(trace.manifest.status).toBe("cancelled")
    expect(trace.manifest.shutdown_signal).toBe("SIGTERM")
    expect(trace.nodes.some((node: any) => node.node_id === "signal_listener_order_ready")).toBe(true)
  })

  test("keeps SIGTERM journal finalization exactly once after the case already finished", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-sigterm-after-finish-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigterm-after-finish.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.node({ node_id: "finished_before_signal", kind: "verification", component: "tool", title: "finished before signal" })`,
        `CaseTrace.finish({ status: "success", result: { answer: "complete" } })`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "sigterm-after-finish-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const caseDir = path.join(dir, "sigterm-after-finish-case")
    expect(await waitForExists(path.join(caseDir, "trace.json"), 3000)).toBe(true)

    proc.kill("SIGTERM")
    expect(await proc.exited).toBe(143)
    expect(await new Response(proc.stderr).text()).toBe("")

    const journal = await readCausalIRJournal(caseDir)
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    assertCausalIRJournalAudit(journal)
    assertJournalReplaysCanonicalTrace(journal, trace)
    assertExactlyOneFinalizationAtEnd(journal)
  })

  test("publishes canonical terminal files before compatibility projections", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-canonical-first-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "canonical-first.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `import path from "node:path"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ input: { prompt: "canonical first" } }) as any`,
        `CaseTrace.node({ node_id: "canonical_first_ready", kind: "verification", component: "runtime", title: "ready" })`,
        `const writes: string[] = []`,
        `const durableWrite = trace.safeWrite.bind(trace)`,
        `trace.safeWrite = (target: string, content: string) => { writes.push(path.relative(trace.caseDir, target)); return durableWrite(target, content) }`,
        `const durableLinkOrWrite = trace.safeLinkOrWrite.bind(trace)`,
        `trace.safeLinkOrWrite = (source: string, target: string, content: string) => { writes.push(path.relative(trace.caseDir, target)); return durableLinkOrWrite(source, target, content) }`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGTERM", signal: "SIGTERM" } })`,
        `fs.writeFileSync(path.join(trace.caseDir, "write-order.json"), JSON.stringify(writes))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "canonical-first-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "canonical-first-case")
    const writes = JSON.parse(await fs.readFile(path.join(caseDir, "write-order.json"), "utf8")) as string[]
    const traceIndex = writes.indexOf("trace.json")
    const manifestIndex = writes.indexOf("manifest.json")
    const partialIndex = writes.indexOf(path.join("partial", "latest.json"))
    const provenanceIndex = writes.indexOf("provenance-trace.json")
    const legacyIndex = writes.indexOf("legacy-trace.json")
    const htmlIndex = writes.indexOf("trace.html")

    expect(traceIndex).toBeGreaterThanOrEqual(0)
    expect(manifestIndex).toBeGreaterThan(traceIndex)
    expect(partialIndex).toBeGreaterThan(manifestIndex)
    expect(provenanceIndex).toBeGreaterThan(partialIndex)
    expect(legacyIndex).toBeGreaterThan(partialIndex)
    expect(htmlIndex).toBeGreaterThan(partialIndex)

    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const finalization = (await readCausalIRJournal(caseDir)).at(-1) as any
    expect(trace.manifest.status).toBe("cancelled")
    expect(finalization.operation).toBe("case.finalized")
    expect(finalization.data.snapshot).toBeUndefined()
    expect(finalization.data.trace).toBeUndefined()
    expect(Buffer.byteLength(JSON.stringify(finalization))).toBeLessThan(16 * 1024)
    if (process.platform !== "win32") {
      const canonicalStat = await fs.stat(path.join(caseDir, "trace.json"))
      const partialStat = await fs.stat(path.join(caseDir, "partial", "latest.json"))
      expect(partialStat.dev).toBe(canonicalStat.dev)
      expect(partialStat.ino).toBe(canonicalStat.ino)
    }
  })

  test("promotes an inferred final response when the exit gate confirms completion before shutdown", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v57-exit-finality-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "exit-finality-v57.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "server completed case" }, environment: { model: "unit-test" } })`,
        `const response = CaseTrace.responseOutput({ text: "Final answer: src/pricing.mjs uses a 15% cap and npm test passed.", metadata: { sessionID: "ses_exit", messageID: "msg_final", partID: "part_final" } })`,
        `CaseTrace.exitGate({ session_id: "ses_exit", message_id: "msg_final", has_final_answer: true, needs_compaction: false, auto_continue: false, synthetic_continue: false, continuation_source: "none", decision: "exit", reason: "assistant_finished_without_pending_tools", source_refs: response ? ["response_segment:" + response.segment_id] : [] })`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGTERM", signal: "SIGTERM" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "exit-finality-v57-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "exit-finality-v57-case", "trace.json"), "utf8")) as any
    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "exit-finality-v57-case", "manifest.json"), "utf8"),
    ) as any
    const caseRecord = trace.records.find((record: any) => record.event_type === "case.completed")
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
    const exitGate = trace.records.find((record: any) => record.event_type === "exit.gate")

    expect(manifest.server_status).toBe("cancelled")
    expect(manifest.case_status).toBe("success")
    expect(caseRecord).toBeTruthy()
    expect(response.data.finality_source).toBe("explicit")
    expect(response.data.metadata.finality_reason).toBe("exit_gate_has_final_answer")
    expect(exitGate.source_refs).toContain(`response_segment:${response.data.segment_id}`)
    expect(claims.length).toBeGreaterThan(0)
    expect(claims.every((record: any) => record.data.metadata.finality_source === "explicit")).toBe(true)
  })

  test("links recent tool errors to final claims without manual source refs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v54-tool-error-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "tool-error-v54.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "read current requirement" }, environment: { model: "unit-test" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.error", data: { tool: "read", callID: "call_missing", sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "docs/current-requirement.md" }, error: "ENOENT: no such file or directory, open 'docs/current-requirement.md'" } })`,
        `for (let i = 0; i < 14; i++) CaseTrace.event({ component: "tool", event_type: "tool.result", data: { tool: "read", callID: "call_success_" + i, sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "docs/source-" + i + ".md" }, output: "irrelevant source " + i } })`,
        `CaseTrace.responseOutput({ text: "docs/current-requirement.md 文件不存在（工具失败），但测试通过。", metadata: { response_role: "final_answer", visibility: "user_visible", is_final_for_case: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "tool-error-v54-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "tool-error-v54-case", "trace.json"), "utf8")) as any
    const toolError = trace.records.find((record: any) => record.event_type === "tool.error")
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")

    expect(trace.trace_version).toBe("6.0")
    expect(toolError).toBeTruthy()
    expect(toolError.status).toBe("error")
    expect(toolError.data.call_id).toBe("call_missing")
    expect(toolError.data.tool_name).toBe("read")
    expect(toolError.data.error_kind).toBe("file_not_found")
    expect(toolError.data.observed_by_model).toBe(true)
    expect(response.source_refs ?? []).not.toContain("tool_error:call_missing")
    expect(claim.data.support_level).toBe("unsupported")
    expect(claim.data.direct_evidence_refs ?? []).not.toContain("tool_error:call_missing")
    expect(assessment).toBeTruthy()
    expect(assessment.data.claim_id).toBe(claim.data.claim_id)
    expect(assessment.data.tool_failure_context_refs).toContain("tool_error:call_missing")
    expect(assessment.data.tool_failure_dependency_refs ?? []).not.toContain("tool_error:call_missing")
    expect(assessment.data.support_level).toBe("unsupported")
    const advisoryEdges = temporalAdvisoryEdgesForMember(trace, "tool_error:call_missing")
    expect(advisoryEdges.length).toBeGreaterThan(0)
    expect(
      advisoryEdges.every(
        (edge: any) => edge.evidence_tier === "temporal_advisory" && edge.eligible_for_attribution === false,
      ),
    ).toBe(true)
  })

  test("records tool failure handling provenance when replacement evidence supports a claim", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v59-tool-failure-handling-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "tool-failure-handling-v59.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "read the current requirement and report cap" }, environment: { model: "unit-test" } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.error", data: { tool: "read", callID: "call_missing", sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "docs/current-requirement.md" }, error: "ENOENT: no such file or directory, open 'docs/current-requirement.md'" } })`,
        `const replacement = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "docs/architecture.md says the renewal discount cap is 15 percent", data: { path: "docs/architecture.md", output: "docs/current-requirement.md moved to docs/architecture.md. The renewal discount cap is 15 percent." } })`,
        `CaseTrace.responseOutput({ text: "docs/current-requirement.md was missing, so I used docs/architecture.md; the renewal discount cap is 15 percent.", source_refs: replacement ? ["evidence:" + replacement.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "tool-failure-handling-v59-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "tool-failure-handling-v59-case", "trace.json"), "utf8"),
    ) as any
    const toolError = trace.records.find((record: any) => record.event_type === "tool.error")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")

    expect(toolError.data.handled_status).toBe("recovered_with_replacement_evidence")
    expect(toolError.data.replacement_evidence_refs.length).toBeGreaterThan(0)
    expect(toolError.data.downstream_claim_refs.length).toBeGreaterThan(0)
    expect(assessment.data.tool_failure_dependency_refs ?? []).not.toContain("tool_error:call_missing")
    expect(assessment.data.tool_failure_handled_status).toBe("recovered_with_replacement_evidence")
    expect(assessment.data.tool_failure_context_refs).toContain("tool_error:call_missing")
    expect(assessment.data.replacement_evidence_refs.length).toBeGreaterThan(0)
  })

  test("backfills tool result provenance through observations and claim support", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v54-tool-result-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "tool-result-v54.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "confirm discount cap" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "read", input: { callID: "call_read", args: { filePath: "src/pricing.mjs" } } })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "src/pricing.mjs says renewalQuote discount cap is 15 percent", data: { path: "src/pricing.mjs", output: "9: The renewalQuote discount cap is 15 percent." }, source_refs: span ? ["span:" + span.id] : [] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", span_id: span?.id, data: { tool: "read", callID: "call_read", sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "src/pricing.mjs" }, output: "9: The renewalQuote discount cap is 15 percent." } })`,
        `span?.end({ output: { output: "9: The renewalQuote discount cap is 15 percent." } })`,
        `CaseTrace.responseOutput({ text: "renewalQuote discount cap is 15 percent.", source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "tool-result-v54-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "tool-result-v54-case", "trace.json"), "utf8")) as any
    const toolResult = trace.records.find((record: any) => record.event_type === "tool.result")
    const observation = trace.records.find(
      (record: any) => record.event_type === "execution.observation" && record.title === "file_read",
    )
    const evidence = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")

    expect(trace.trace_version).toBe("6.0")
    expect(toolResult).toBeTruthy()
    expect(observation.source_refs).toContain("tool_result:call_read")
    expect(evidence.source_refs).toContain("tool_result:call_read")
    expect(claim.data.direct_evidence_refs).toContain("tool_result:call_read")
    expect(assessment.data.tool_result_dependency_refs).toContain("tool_result:call_read")
  })

  test("preserves tool-result backfill when a semantic fact is duplicated", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-store-owned-semantic-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "store-owned-semantic.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "confirm pricing owner" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "read", input: { callID: "call_owner", args: { filePath: "src/pricing.mjs" } } })`,
        `const data = { subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 7, line_end: 7, output: "renewalQuote owner is billing-platform" }`,
        `CaseTrace.evidenceFact({ source: "read", category: "repo_fact", summary: "pricing owner", data, source_refs: span ? ["span:" + span.id] : [] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", span_id: span?.id, data: { tool: "read", callID: "call_owner", args: { filePath: "src/pricing.mjs" }, output: data.output } })`,
        `CaseTrace.evidenceFact({ source: "read", category: "repo_fact", summary: "pricing owner", data, source_refs: ["context:duplicate_semantic_fact"] })`,
        `span?.end({ output: { output: data.output } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "store-owned-semantic-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "store-owned-semantic-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const journal = (await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    const causalJournal = journal.filter((entry: any) => entry.operation)
    const fact = trace.nodes.find((node: any) => node.kind === "evidence.semantic_fact")
    const factUpdates = causalJournal.filter(
      (entry: any) => entry.operation === "node.updated" && entry.entity_id === fact.node_id,
    )
    const latestFact = factUpdates.at(-1)?.data

    expect(fact.data.occurrence_count).toBe(2)
    expect(fact.legacy_source_refs).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^span:/),
        "tool_result:call_owner",
        "context:duplicate_semantic_fact",
      ]),
    )
    expect(fact.data.tool_outcome_refs).toContain("tool_result:call_owner")
    expect(latestFact.source_refs).toEqual(fact.source_refs)
    expect(latestFact.legacy_source_refs).toEqual(expect.arrayContaining(fact.legacy_source_refs))
    expect(latestFact.data.tool_outcome_refs).toContain("tool_result:call_owner")
    assertCausalIRJournalAudit(causalJournal)
    expect(replayCausalIRJournal(causalJournal).nodes).toEqual(trace.nodes)
  })

  test("keeps candidate tool outcomes out of claim dependency refs when they do not match", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v55-focused-tool-result-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "focused-tool-result-v55.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "confirm discount cap" }, environment: { model: "unit-test" } })`,
        `const discountSpan = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "read", input: { callID: "call_discount", args: { filePath: "src/pricing.mjs" } } })`,
        `const ownerSpan = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "read", input: { callID: "call_owner", args: { filePath: "docs/architecture.md" } } })`,
        `const discountObs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "src/pricing.mjs says renewalQuote discount cap is 15 percent", data: { path: "src/pricing.mjs", output: "9: The renewalQuote discount cap is 15 percent." }, source_refs: discountSpan ? ["span:" + discountSpan.id] : [] })`,
        `const ownerObs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "docs/architecture.md says renewalQuote owner is billing-platform", data: { path: "docs/architecture.md", output: "3: renewalQuote owner is billing-platform." }, source_refs: ownerSpan ? ["span:" + ownerSpan.id] : [] })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", span_id: discountSpan?.id, data: { tool: "read", callID: "call_discount", sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "src/pricing.mjs" }, output: "9: The renewalQuote discount cap is 15 percent." } })`,
        `CaseTrace.event({ component: "tool", event_type: "tool.result", span_id: ownerSpan?.id, data: { tool: "read", callID: "call_owner", sessionID: "ses_tool", messageID: "msg_tool", args: { filePath: "docs/architecture.md" }, output: "3: renewalQuote owner is billing-platform." } })`,
        `discountSpan?.end({ output: { output: "9: The renewalQuote discount cap is 15 percent." } })`,
        `ownerSpan?.end({ output: { output: "3: renewalQuote owner is billing-platform." } })`,
        `CaseTrace.responseOutput({ text: "renewalQuote discount cap is 15 percent.", source_refs: discountObs && ownerObs ? ["observation:" + discountObs.node_id, "observation:" + ownerObs.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "focused-tool-result-v55-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "focused-tool-result-v55-case", "trace.json"), "utf8"),
    ) as any
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")

    expect(trace.trace_version).toBe("6.0")
    expect(claim.data.candidate_tool_outcome_refs).toContain("tool_result:call_discount")
    expect(claim.data.candidate_tool_outcome_refs).toContain("tool_result:call_owner")
    expect(claim.data.dependency_tool_outcome_refs).toContain("tool_result:call_discount")
    expect(claim.data.dependency_tool_outcome_refs).not.toContain("tool_result:call_owner")
    expect(assessment.data.tool_result_dependency_refs).toContain("tool_result:call_discount")
    expect(assessment.data.tool_result_dependency_refs).not.toContain("tool_result:call_owner")
  })

  test("does not extract design records from short progress or result summaries", async () => {
    const { shouldExtractDesignRecordForResponse, shouldExtractDesignRecordForResponseSegment } = await import(
      "@/session/processor"
    )
    const designLikeSummary = [
      "Here is a complete summary of everything related to `renewalQuote`.",
      "## Overall Design and Architecture",
      "The architecture doc specifies design constraints: general discount logic, no hardcoding test inputs, and no public API bypass.",
      "The implementation uses the pricing module and tests verify the expected result.",
    ].join("\\n")

    expect(
      shouldExtractDesignRecordForResponse(
        "测试通过。修复总结：\\n| 来源 | 状态 |\\n| docs/current-requirement.md | 文件不存在 |\\n| src/pricing.mjs:6 修复 | 0.2 -> 0.15 |",
      ),
    ).toBe(false)
    expect(
      shouldExtractDesignRecordForResponse(
        [
          "方案设计：在 pricing 层新增折扣上限策略。",
          "架构边界：pricing 负责报价，payment 不参与本次变更。",
          "取舍：保持 API 稳定，仅替换内部策略。",
          "风险：历史订单回放需要验证。",
          "测试策略：补充 pricing 单测和回归测试。",
        ].join("\\n"),
      ),
    ).toBe(true)
    expect(
      shouldExtractDesignRecordForResponse(
        [
          "All tests pass. Here's the summary:",
          "Design constraints satisfied:",
          "General calculation logic -- the cap is a constant applied uniformly; no special-casing.",
          "No hardcoded test inputs -- the source contains no test fixture values.",
          "Public API intact -- renewalQuote(input) signature unchanged.",
          "Cap at 15% -- per docs/architecture.md.",
        ].join("\\n"),
      ),
    ).toBe(true)
    expect(
      shouldExtractDesignRecordForResponse(
        [
          "Both tests pass.",
          "修复内容：将 src/pricing.mjs:6 的折扣上限从 0.2 改为 0.15。",
          "设计约束说明：",
          "通用计算逻辑：根据 loyaltyYears 和 seats 动态计算折扣。",
          "禁止硬编码：源码中不存在 baseCents === 1200 或 return 51000。",
          "禁止绕过 public API：所有逻辑通过 renewalQuote(input) 暴露。",
        ].join("\\n"),
      ),
    ).toBe(true)
    expect(shouldExtractDesignRecordForResponse(designLikeSummary)).toBe(true)
    expect(
      shouldExtractDesignRecordForResponseSegment(designLikeSummary, {
        response_role: "intermediate_summary",
        visibility: "user_visible",
        is_final_for_case: false,
      }),
    ).toBe(false)
    expect(
      shouldExtractDesignRecordForResponseSegment(designLikeSummary, {
        response_role: "final_answer",
        visibility: "user_visible",
        is_final_for_case: true,
      }),
    ).toBe(true)
  })

  test("routes plan state and semantic evidence to separate formal records", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v53-evidence-hygiene-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "evidence-hygiene-v53.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "todowrite", category: "tool_output", summary: "2 todos", data: { args: { todos: [{ content: "read docs", status: "completed" }, { content: "run tests", status: "in_progress" }] }, output: "todo list" } })`,
        `CaseTrace.evidenceFact({ source: "mcp", category: "syntheticFacts:repo_fact", summary: "MCP returned owner", data: { server: "syntheticFacts", tool: "repo_fact", args: { topic: "quote-owner" }, content: [{ type: "text", text: "{\\"subject\\":\\"renewalQuote\\",\\"predicate\\":\\"owner\\",\\"value\\":\\"billing-platform\\",\\"path\\":\\"src/pricing.mjs\\",\\"line_start\\":10,\\"line_end\\":12}" }] } })`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "plain tool output", data: { output: "routine progress with no stable fact" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "evidence-hygiene-v53-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "evidence-hygiene-v53-case", "trace.json"), "utf8"),
    ) as any
    const plan = trace.records.find((record: any) => record.event_type === "task.plan_state")
    const semantic = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")
    const execution = trace.records.find((record: any) => record.event_type === "execution.observation")
    const legacyTodoFact = trace.records.find(
      (record: any) => record.event_type === "evidence.fact" && record.data?.source === "todowrite",
    )
    const semanticTodoFact = trace.records.find(
      (record: any) => record.event_type === "evidence.semantic_fact" && record.data?.source === "todowrite",
    )

    expect(plan).toBeTruthy()
    expect(plan.data.plan_items.total).toBe(2)
    expect(plan.data.plan_items.completed).toBe(1)
    expect(plan.data.plan_items.in_progress).toBe(1)
    expect(semantic).toBeTruthy()
    expect(semantic.data.fact_kind).toBe("mcp_fact")
    expect(semantic.data.structured_claim).toMatchObject({
      subject: "renewalQuote",
      predicate: "owner",
      value: "billing-platform",
    })
    expect(execution).toBeTruthy()
    expect(legacyTodoFact).toBeUndefined()
    expect(semanticTodoFact).toBeUndefined()
    expect(trace.metrics.trace_health.duplicate_semantic_facts).toBe(0)
  })

  test("keeps response claim source refs narrow and folds legacy refs into data", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v53-claim-refs-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "claim-refs-v53.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const evidence = CaseTrace.evidenceFact({ source: "mcp", category: "syntheticFacts:repo_fact", summary: "owner billing-platform", data: { subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `CaseTrace.contextTransform({ stage: "llm_request_ready", session_id: "ses_claim", input: { message: "large context" }, output: { message: "large context" } })`,
        `CaseTrace.responseOutput({ text: "Owner is billing-platform.", source_refs: ["prompt:node_prompt", "context:node_context", "tool_span:span_tool", evidence ? "evidence:" + evidence.node_id : "evidence:missing"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "claim-refs-v53-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "claim-refs-v53-case", "trace.json"), "utf8")) as any
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")

    expect(claim.source_refs).toEqual(claim.data.direct_evidence_refs)
    expect(claim.source_refs.every((ref: string) => ref.startsWith("evidence:"))).toBe(true)
    expect(claim.data.legacy_context_refs).toContain("context:node_context")
    expect(claim.data.legacy_context_refs).toContain("tool_span:span_tool")
    expect(claim.data.attribution_summary).toMatchObject({
      direct_evidence_count: 1,
      legacy_context_count: 3,
      support_level: "direct",
    })
    expect(trace.metrics.trace_health.legacy_context_ref_claims).toBe(0)
  })

  test("summarizes inline subagent refs and compaction retention facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v53-summaries-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "summaries-v53.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "summarize docs" } })`,
        `span?.end({ output: { child_session_id: "ses_child_summary", child_status: "success", output: "child result" } })`,
        `CaseTrace.promptAssembly({ stage: "subagent_prompt", session_id: "ses_child_summary", agent: "general", input: { prompt: "summarize" }, output: { message_id: "msg_child_user" } })`,
        `CaseTrace.evidenceFact({ source: "mcp", category: "syntheticFacts:repo_fact", summary: "child owner fact", data: { session_id: "ses_child_summary", subject: "renewalQuote", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 10, line_end: 12 } })`,
        `CaseTrace.responseOutput({ response_role: "subagent_result", text: "Child result: owner is billing-platform.", metadata: { session_id: "ses_child_summary", message_id: "msg_child_assistant" } })`,
        `CaseTrace.responseOutput({ text: "Parent answer consumed child result: owner is billing-platform.", source_refs: span ? ["tool_span:" + span.id] : [] })`,
        `CaseTrace.compaction({ trigger: "auto", provider_id: "deepseek", model_id: "unit-test", output_summary: "summary", serialized_tail: "tail", auto_continue: true, source_refs: ["context:ctx_before"], context_ledger: { algorithm: "head-tail-summary", token_estimate_before: 1200, token_estimate_after: 300, retained_message_ids: ["msg_keep"], dropped_message_ids: ["msg_drop"], retained_fact_refs: ["evidence:fact_keep"], dropped_fact_refs: ["evidence:fact_drop"], auto_continue_prompt_ref: "message:msg_continue" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "summaries-v53-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "summaries-v53-case", "trace.json"), "utf8")) as any
    const subagent = trace.records.find((record: any) => record.event_type === "subagent.call")
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(subagent.data.child_trace_available).toBe(true)
    expect(subagent.data.child_trace_mode).toBe("inline_same_trace")
    expect(subagent.data.child_record_refs.length).toBeLessThanOrEqual(20)
    expect(subagent.data.child_trace_artifact_ref).toBeTruthy()
    expect(subagent.data.child_timeline_summary.record_count).toBeGreaterThan(0)
    expect(subagent.data.child_metric_summary.semantic_evidence_count).toBeGreaterThan(0)
    expect(subagent.data.child_key_evidence_refs.length).toBeGreaterThan(0)
    expect(subagent.data.child_output_preview).toContain("child result")
    expect(subagent.data.child_key_fact_refs.length).toBeGreaterThan(0)
    expect(subagent.data.parent_consumption_refs.length).toBeGreaterThan(0)
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.id === subagent.record_id && edge.to.type === "response_segment" && edge.relation === "reported_to",
      ),
    ).toBe(true)
    expect(compaction.data.retention_ratio).toBe(0.25)
    expect(compaction.data.retained_fact_count).toBe(1)
    expect(compaction.data.dropped_fact_count).toBe(1)
    expect(compaction.data.compression_loss_risks).toContain("dropped_semantic_facts")
  })

  test("finalizes trace.json and trace.html when a traced process exits", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "exit-with-active-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello" } })`,
        `process.exit(0)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "exit-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)
    expect(await exists(path.join(dir, "exit-case", "events.jsonl"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "legacy-trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "trace.html"))).toBe(true)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "exit-case", "legacy-trace.json"), "utf8"),
    ) as TraceSummary
    expect(trace.status).toBe("success")
    expect(trace.events.some((event) => event.event_type === "turn.start")).toBe(true)
  })

  test("finalizes trace.json and trace.html when a traced process receives SIGTERM", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-sigterm-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigterm-with-active-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello from sigterm" } })`,
        `CaseTrace.node({ node_id: "fixture_signal_sigterm_legacy_ready", kind: "verification", component: "runtime", title: "SIGTERM legacy fixture readiness marker" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "sigterm-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })

    const caseDir = path.join(dir, "sigterm-case")
    const persistedJournal = await waitForCompleteCausalIRCheckpoint(caseDir, "fixture_signal_sigterm_legacy_ready")
    expect(persistedJournal).toBeDefined()
    assertCausalIRJournalAudit(persistedJournal!)
    proc.kill("SIGTERM")
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(143)
    expect(await exists(path.join(dir, "sigterm-case", "trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "sigterm-case", "legacy-trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "sigterm-case", "trace.html"))).toBe(true)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "sigterm-case", "legacy-trace.json"), "utf8"),
    ) as TraceSummary
    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "sigterm-case", "trace.json"), "utf8"),
    ) as ProvenanceTraceSummary

    expect(trace.status).toBe("cancelled")
    expect(provenance.manifest.server_status).toBe("cancelled")
    expect(provenance.manifest.case_status).toBe("cancelled")
  })

  test("keeps a partial trace.html snapshot available before an uncapturable SIGKILL", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-sigkill-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigkill-with-active-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello from sigkill" } })`,
        `CaseTrace.node({ node_id: "fixture_signal_sigkill_ready", kind: "verification", component: "runtime", title: "SIGKILL fixture readiness marker" })`,
        `;(CaseTrace.get() as any).writePartial(true)`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "sigkill-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })

    const caseDir = path.join(dir, "sigkill-case")
    const persistedJournal = await waitForCompleteCausalIRCheckpoint(caseDir, "fixture_signal_sigkill_ready")
    expect(persistedJournal).toBeDefined()
    assertCausalIRJournalAudit(persistedJournal!)
    expect(await waitForExists(path.join(caseDir, "partial", "latest.json"))).toBe(true)
    expect(await waitForExists(path.join(caseDir, "trace.html"))).toBe(true)
    proc.kill("SIGKILL")
    await proc.exited.catch(() => undefined)

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)

    expect(html).toContain("Trace v6.0")
    expect(partial.manifest.server_status).toBe("running")
    assertCausalIRJournalAudit(journal)
    assertJournalReplaysCanonicalTrace(journal, partial)
    expect(journal.filter((entry: any) => entry.operation === "case.checkpointed").length).toBeGreaterThan(0)
    expect(journal.some((entry: any) => entry.operation === "case.finalized")).toBe(false)
  })

  test("renders component data flow and agent process sections", () => {
    const trace: TraceSummary = {
      trace_version: "1.0",
      case_id: "visual-case",
      run_id: "run_visual",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:01.000Z",
      duration_ms: 1000,
      status: "success",
      environment: {},
      token_usage: { input: 10, output: 20, total: 30 },
      errors: [],
      result: { exit_code: 0 },
      spans: [
        {
          span_id: "span_run",
          component: "runtime",
          operation: "turn",
          name: "interactive.turn",
          status: "success",
          start_time: "2026-06-27T00:00:00.000Z",
          start_ms: 0,
          end_time: "2026-06-27T00:00:00.200Z",
          end_ms: 200,
          duration_ms: 200,
          input_summary: { type: "text", preview: "实现一个需求" },
          output_summary: { type: "object", preview: '{"queue":0}' },
        },
        {
          span_id: "span_llm",
          component: "llm",
          operation: "stream",
          name: "openai/gpt-test",
          status: "success",
          start_time: "2026-06-27T00:00:00.220Z",
          start_ms: 220,
          end_time: "2026-06-27T00:00:00.900Z",
          end_ms: 900,
          duration_ms: 680,
          input_summary: { type: "object", preview: '{"message_count":3}' },
          output_summary: { type: "object", preview: '{"completed":true}' },
          token_usage: { input: 10, output: 20, total: 30 },
        },
      ],
      events: [
        {
          event_id: "evt_1",
          component: "runtime",
          event_type: "turn.send",
          timestamp: "2026-06-27T00:00:00.010Z",
          time_ms: 10,
        },
        {
          event_id: "evt_2",
          span_id: "span_llm",
          component: "llm",
          event_type: "stream.text-delta",
          timestamp: "2026-06-27T00:00:00.300Z",
          time_ms: 300,
        },
      ],
    }

    const html = renderCaseTraceHtml(trace)

    expect(html).toContain("Agent 运行流程")
    expect(html).toContain("组件数据流转")
    expect(html).toContain("runtime")
    expect(html).toContain("llm")
  })

  test("renders all agent process items with scrollable input and output cells", () => {
    const spans = Array.from({ length: 130 }, (_, index) => {
      const item = index + 1
      return {
        span_id: `span_${item}`,
        component: "runtime" as const,
        operation: "turn",
        name: `interactive.turn.${item}`,
        status: "success" as const,
        start_time: "2026-06-27T00:00:00.000Z",
        start_ms: item * 10,
        end_time: "2026-06-27T00:00:00.010Z",
        end_ms: item * 10 + 5,
        duration_ms: 5,
        input_summary: {
          type: "text",
          preview: `input-${item}-` + "x".repeat(320),
        },
        output_summary: {
          type: "text",
          preview: `output-${item}-` + "y".repeat(320),
        },
      }
    })

    const trace: TraceSummary = {
      trace_version: "1.0",
      case_id: "all-process-case",
      run_id: "run_all_process",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:02.000Z",
      duration_ms: 2000,
      status: "success",
      environment: {},
      token_usage: {},
      errors: [],
      spans,
      events: [],
    }

    const html = renderCaseTraceHtml(trace)

    expect(html).toContain("interactive.turn.1")
    expect(html).toContain("interactive.turn.130")
    expect(html).not.toContain("已展示前 120 条")
    expect(html).toContain('class="process-scroll"')
    expect(html).toContain('class="io-scroll"')
  })

  test("renders v6.0 provenance report with flow-style sections and scrollable IO panes", () => {
    const trace: ProvenanceTraceSummary = {
      trace_version: "6.0",
      manifest: {
        trace_version: "6.0",
        case_id: "viewer-v46-case",
        run_id: "run_viewer_v46",
        started_at: "2026-06-30T00:00:00.000Z",
        ended_at: "2026-06-30T00:00:01.000Z",
        duration_ms: 1000,
        status: "success",
        collection_mode: "passive_sidecar",
        behavior_impact: "none",
        input: { prompt: "fix pricing" },
        environment: { model: "deepseek/deepseek-v4-pro" },
        token_usage: { input: 10, output: 5, total: 15 },
        files: {
          trace: "trace.json",
          legacy_trace: "legacy-trace.json",
          provenance_trace: "provenance-trace.json",
          trace_html: "trace.html",
          records: "records.jsonl",
          raw_events: "raw-events.jsonl",
          partial_latest: "partial/latest.json",
        },
      },
      records: [
        {
          record_id: "llm_1",
          component: "llm",
          event_type: "llm.call",
          timestamp: "2026-06-30T00:00:00.100Z",
          time_ms: 100,
          title: "deepseek/deepseek-v4-pro",
          status: "success",
          duration_ms: 500,
          token_usage: { input: 10, output: 5, total: 15 },
          data: {
            input: { prompt: "fix pricing" },
            output: { text: "need tools" },
            agent: "build",
            provider_id: "deepseek",
            model_id: "deepseek-v4-pro",
          },
        },
        {
          record_id: "mcp_1",
          component: "mcp",
          event_type: "mcp.call",
          timestamp: "2026-06-30T00:00:00.300Z",
          time_ms: 300,
          title: "trace-facts:audit_facts",
          status: "success",
          source_locations: [{ path: "src/pricing.mjs", line_start: 1, line_end: 20 }],
          typed_resources: [
            {
              type: "repo_fact",
              key: "pricing-owner",
              fact: "pricing.mjs owns coupon math.",
              source_location: { path: "src/pricing.mjs", line_start: 1, line_end: 20 },
            },
          ],
          data: {
            input: { tool: "audit_facts" },
            output: { fact: "pricing.mjs owns coupon math." },
          },
        },
        {
          record_id: "resp_1",
          component: "result",
          event_type: "response.output",
          timestamp: "2026-06-30T00:00:00.900Z",
          time_ms: 900,
          title: "Response output 1",
          source_refs: ["tool_span:span_1"],
          data: {
            text: "pricing bug is line 5",
            response_role: "final_answer",
            is_final_for_case: true,
          },
        },
        {
          record_id: "evidence_1",
          component: "mcp",
          event_type: "evidence.semantic_fact",
          timestamp: "2026-06-30T00:00:00.820Z",
          time_ms: 820,
          title: "repo_fact",
          status: "success",
          source_refs: ["mcp:mcp_1"],
          source_locations: [{ path: "src/pricing.mjs", line_start: 5, line_end: 5 }],
          data: {
            fact_kind: "mcp_fact",
            canonical_subject: "pricing",
            claim: "pricing bug is line 5",
            structured_claim: {
              subject: "pricing",
              predicate: "bug_location",
              value: "line 5",
              extraction_method: "mcp_json_text",
              source_span: { path: "src/pricing.mjs", line_start: 5, line_end: 5 },
            },
            quality_flags: [],
          },
        },
        {
          record_id: "claim_1",
          component: "result",
          event_type: "response.claim",
          timestamp: "2026-06-30T00:00:00.920Z",
          time_ms: 920,
          title: "Response claim 1",
          status: "success",
          source_refs: ["evidence:evidence_1", "tool_span:span_1"],
          data: {
            text: "pricing bug is line 5",
            response_segment_id: "segment_1",
            claim_index: 1,
            direct_evidence_refs: ["evidence:evidence_1"],
            context_refs: [],
            execution_refs: ["tool_span:span_1"],
            support_level: "direct",
            quality_flags: [],
          },
        },
      ],
      dataflow_edges: [
        {
          edge_id: "edge_1",
          from: { type: "node", id: "mcp_1" },
          to: { type: "node", id: "resp_1" },
          relation: "consumed",
          label: "MCP fact used by final response",
        },
        {
          edge_id: "edge_2",
          from: { type: "evidence", id: "evidence_1" },
          to: { type: "response_claim", id: "claim_1" },
          relation: "supports_claim",
          label: "Evidence supports response claim",
        },
      ],
      artifacts: [
        {
          artifact_id: "artifact_1",
          kind: "text",
          label: "llm.output",
          path: "artifacts/sha256/artifact_1.txt",
          length: 120,
          hash: "hash",
          preview: "artifact preview",
          created_at: "2026-06-30T00:00:00.000Z",
        },
      ],
      metrics: {
        spans: 1,
        events: 2,
        records: 5,
        dataflow_edges: 2,
        artifacts: 1,
        token_usage: { input: 10, output: 5, total: 15 },
        trace_health: {
          circular_reference_markers: 0,
          open_records: 1,
          finalized_open_records: 1,
          expected_lifecycle_finalized_records: 1,
          unexpected_missing_close_records: 0,
          llm_turns_missing_token_usage: 0,
          llm_turns_missing_finish_reason: 0,
          compaction_quality_flags: {},
          empty_subagent_results: 0,
          broad_response_refs: 0,
          duplicate_evidence_facts: 0,
          duplicate_semantic_facts: 0,
          generic_evidence_facts: 0,
          generic_semantic_facts: 0,
          unsupported_response_claims: 0,
          context_only_response_claims: 0,
          execution_observations: 0,
          task_plan_states: 0,
          payload_duplication_groups: 0,
          compaction_check_missing: 0,
          issues: [],
        },
      },
    }

    const html = renderProvenanceTraceHtml(trace)

    for (const id of [
      "overview",
      "trace-health",
      "semantic-pipeline",
      "llm-turns",
      "lifecycle",
      "subagents",
      "claim-evidence-matrix",
      "evidence-facts",
      "execution-observations",
      "agent-flow",
      "component-dataflow",
      "io-inspector",
      "semantic-facts",
      "context-compaction",
      "artifacts",
    ]) {
      expect(html).toContain(`id="${id}"`)
    }
    expect(html).toContain("Trace v6.0")
    expect(html).toContain("Trace Health")
    expect(html).toContain("Claim Evidence Matrix")
    expect(html).toContain("structured_claim")
    expect(html).toContain('class="io-grid"')
    expect(html).toContain('class="io-input"')
    expect(html).toContain('class="io-output"')
    expect(html).toContain("repo_fact")
    expect(html).toContain("pricing-owner")
    expect(html).toContain("final_answer")
  })

  test("stores large semantic payloads as artifacts and keeps trace.json lightweight", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-artifact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "large-trace-payload.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const payload = "semantic-context-before-compaction:" + "x".repeat(5000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "context", event_type: "context.before_compaction", data: { payload: ${JSON.stringify(payload)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "artifact-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "128",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "artifact-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any

    expect(trace.artifacts.length).toBeGreaterThanOrEqual(1)
    expect(JSON.stringify(trace).includes(payload)).toBe(false)

    const artifact = trace.artifacts.find((item: any) => item.label === "context.context.before_compaction.data")
    expect(artifact).toBeTruthy()
    const artifactText = await fs.readFile(path.join(caseDir, artifact.path), "utf8")
    expect(artifactText).toContain(payload)

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Artifacts")
  })

  test("writes a redacted and byte-verifiable artifact semantic slice manifest", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-artifact-manifest-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "artifact-manifest.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const secret = "sk-artifact-secret"
    const payload = `token=${secret} ${"😀".repeat(80)}`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "context", event_type: "context.before_compaction", data: { payload: ${JSON.stringify(payload)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "artifact-manifest-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "30",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "artifact-manifest-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const artifact = trace.artifacts.find((item: any) => item.label === "context.context.before_compaction.data")
    const artifactText = await fs.readFile(path.join(caseDir, artifact.path), "utf8")
    const slices = artifact.semantic_slices as Array<{
      byte_range: [number, number]
      content: string
      hash: string
      truncated: boolean
    }>

    expect(artifact.availability).toBe("bundled")
    expect(artifact.content_hash).toBe(artifact.hash)
    expect(artifact.content_hash).toBe(createHash("sha256").update(artifactText).digest("hex").slice(0, 16))
    expect(artifact.byte_length).toBe(Buffer.byteLength(artifactText))
    expect(artifactText).not.toContain(secret)
    expect(artifactText).toContain("[REDACTED]")
    expect(slices).toHaveLength(1)
    expect(slices.reduce((total, slice) => total + slice.content.length, 0)).toBeLessThanOrEqual(30)

    const slice = slices[0]!
    const sliceBytes = Buffer.from(slice.content, "utf8")
    expect(sliceBytes.toString("utf8")).toBe(slice.content)
    expect(slice.content).toContain("[REDACTED]")
    expect(slice.content).not.toContain(secret)
    expect(slice.byte_range).toEqual([0, sliceBytes.byteLength])
    expect(artifactText.slice(0, slice.content.length)).toBe(slice.content)
    expect(slice.hash).toBe(createHash("sha256").update(slice.content).digest("hex").slice(0, 16))
    expect(slice.truncated).toBe(true)
  })

  test("normalizes unpaired surrogates before artifact hashes and byte ranges", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-artifact-utf8-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "artifact-utf8.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.compaction({ trigger: "auto", output_summary: "x".repeat(5000) + "prefix\\ud800suffix" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "artifact-utf8-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "2048",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await proc.exited).toBe(0)

    const caseDir = path.join(dir, "artifact-utf8-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const artifact = trace.artifacts.find((item: any) => item.label === "compaction.output_summary")
    const bytes = await fs.readFile(path.join(caseDir, artifact.path))
    const content = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
    const slice = artifact.semantic_slices[0]
    const sliceBytes = Buffer.from(slice.content, "utf8")

    expect(content).toContain("prefix�suffix")
    expect(content).not.toContain("\\ud800")
    expect(artifact.byte_length).toBe(bytes.byteLength)
    expect(artifact.content_hash).toBe(createHash("sha256").update(bytes).digest("hex").slice(0, 16))
    expect(slice.byte_range).toEqual([0, sliceBytes.byteLength])
    expect(slice.hash).toBe(createHash("sha256").update(sliceBytes).digest("hex").slice(0, 16))
  })

  test("renders authorized artifact-backed summaries with expandable full content", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-authorized-artifact-html-"))
    const trace = {
      trace_version: "1.0",
      case_id: "artifact-html-case",
      run_id: "run_artifact_html",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:01.000Z",
      duration_ms: 1000,
      status: "success",
      environment: {},
      token_usage: {},
      errors: [],
      artifacts: [
        {
          artifact_id: "artifact_1",
          kind: "text",
          label: "llm.final_model_messages",
          path: "artifacts/artifact_1.txt",
          length: 55,
          hash: "hash",
          preview: "short preview",
          created_at: "2026-06-27T00:00:00.000Z",
        },
      ],
      spans: [
        {
          span_id: "span_1",
          component: "llm",
          operation: "stream",
          name: "model call",
          status: "success",
          start_time: "2026-06-27T00:00:00.000Z",
          start_ms: 0,
          end_time: "2026-06-27T00:00:00.010Z",
          end_ms: 10,
          duration_ms: 10,
          input_summary: {
            type: "text",
            length: 55,
            hash: "hash",
            preview: "short preview",
            artifact_id: "artifact_1",
          },
        },
      ],
      events: [],
    } as TraceSummary

    try {
      await fs.mkdir(path.join(dir, "artifacts"), { recursive: true })
      await fs.writeFile(path.join(dir, "artifacts/artifact_1.txt"), "authoritative artifact body")
      const html = renderCaseTraceHtml(trace, {
        artifactDir: dir,
        artifactContents: new Map([["artifact_1", "full semantic model messages payload"]]),
      } as any)

      expect(html).toContain("Artifacts")
      expect(html).toContain("查看完整内容")
      expect(html).toContain("full semantic model messages payload")
      const snapshotDigest = createHash("sha256").update("authoritative artifact body").digest("hex")
      expect(html).toContain(`href="artifacts/render-snapshots/sha256/${snapshotDigest}"`)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("persists semantic trace records with artifacts and redaction", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-semantic-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const secret = "sk-test-secret-value"
    const longMessage = "semantic model message: " + "x".repeat(5000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/test" })`,
        `CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "deepseek-v4-pro", agent: "build", message_count: 1, system_count: 1, tool_count: 1, token_estimate: 128, messages: [{ role: "user", content: ${JSON.stringify(longMessage)}, apiKey: ${JSON.stringify(secret)} }], system: ["system prompt"], tools: { bash: { description: "run command", authorization: "Bearer abc" } } })`,
        `CaseTrace.decision({ span_id: span?.id, component: "llm", decision_type: "tool_call", intent: "run tests", chosen_action: "bash", rationale: "Need verification", source_refs: ["ctx_1"] })`,
        `CaseTrace.verification({ span_id: span?.id, tool_call_id: "call_1", command: "node test.js", cwd: "/tmp/project", purpose: "Run unit tests", stage: "baseline", exit_code: 1, status: "failed", stdout: "Error: expected 170, got 30", stderr: "" })`,
        `CaseTrace.change({ span_id: span?.id, tool_call_id: "call_2", files: ["src/pricing.mjs"], intent: "Fix discount formula", diff: "- old\\\\n+ new", source_refs: ["ver_1"] })`,
        `CaseTrace.constraint({ source: "user", constraint: "do not modify files", status: "observed_satisfied", source_refs: ["span_1"] })`,
        `CaseTrace.responseOutput({ response_artifact: "artifact_final", text: "The formula returned discount amount instead of discounted price.", source_refs: ["ver_1", "chg_1"] })`,
        `CaseTrace.edge({ from: { type: "verification", id: "ver_1" }, to: { type: "change", id: "chg_1" }, relation: "failure_to_change", label: "test failure led to edit" })`,
        `span?.end({ output: { completed: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "128",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "semantic-case")
    const traceText = await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")
    const trace = JSON.parse(traceText) as any

    expect(trace.trace_version).toBe("1.3")
    expect(trace.context_snapshots).toHaveLength(1)
    expect(trace.semantic_decisions).toHaveLength(1)
    expect(trace.verification_records).toHaveLength(1)
    expect(trace.change_records).toHaveLength(1)
    expect(trace.constraint_records).toHaveLength(1)
    expect(trace.response_segments).toHaveLength(1)
    expect(trace.dataflow_edges.length).toBeGreaterThanOrEqual(1)
    expect(trace.dataflow_edges.some((edge: any) => edge.relation === "failure_to_change")).toBe(true)
    expect(trace.context_snapshots[0].messages.artifact_id).toBeTruthy()
    expect(trace.verification_records[0].parsed_failures[0]).toMatchObject({ expected: "170", actual: "30" })
    expect(traceText).not.toContain(secret)
    expect(traceText).not.toContain("Bearer abc")
    expect(traceText).not.toContain("final_response_evidence")

    const artifactText = await fs.readFile(path.join(caseDir, trace.artifacts[0].path), "utf8")
    expect(artifactText).toContain("semantic model message")
    expect(artifactText).not.toContain(secret)
    expect(artifactText).not.toContain("Bearer abc")

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Component Dataflow")
    expect(html).toContain("IO Inspector")
    expect(html).toContain("Context Ledger")
  })

  test("preserves explicit token metrics while redacting ambiguous token fields and credentials", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-redaction-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "redaction-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "token-test", metadata: { token_usage: { total: 9 }, apiKey: "sk-test-secret-value" } })`,
        `CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", token_estimate: 128, message_count: 1, metadata: { tokens: 128, token_usage: { input: 3, output: 6 }, authorization: "Bearer abcdefgh" }, messages: [{ role: "user", content: "hello", access_token: "sk-another-secret-value" }] })`,
        `span?.end({ tokenUsage: { inputTokens: 3, outputTokens: 6, totalTokens: 9 } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "redaction-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const traceText = await fs.readFile(path.join(dir, "redaction-case", "legacy-trace.json"), "utf8")
    const trace = JSON.parse(traceText) as TraceSummary

    expect(trace.token_usage.total).toBe(9)
    expect(trace.spans[0].token_usage?.total).toBe(9)
    expect(trace.context_snapshots?.[0]?.token_estimate).toBe(128)
    expect(trace.context_snapshots?.[0]?.metadata?.tokens).toBe("[REDACTED]")
    expect((trace.context_snapshots?.[0]?.metadata as any)?.token_usage).toEqual({ input: 3, output: 6 })
    expect(traceText).not.toContain("sk-test-secret-value")
    expect(traceText).not.toContain("sk-another-secret-value")
    expect(traceText).not.toContain("Bearer abcdefgh")
  })

  test("parses concrete expected and actual values from verification failures", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-failure-parse-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "failure-parse-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const stdout = [
      "file:///tmp/project/test/pricing.test.mjs:11",
      "throw new Error(`expected ${item.expected}, got ${actual}`)",
      "Error: expected 170, got 30",
      "    at file:///tmp/project/test/pricing.test.mjs:11:13",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: "node test/pricing.test.mjs", exit_code: 1, stdout: ${JSON.stringify(stdout)}, stderr: "" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "failure-parse-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "failure-parse-case", "legacy-trace.json"), "utf8"),
    ) as any
    expect(trace.verification_records[0].parsed_failures[0]).toMatchObject({
      message: "Error: expected 170, got 30",
      expected: "170",
      actual: "30",
      file: "file:///tmp/project/test/pricing.test.mjs",
      line: 11,
      column: 13,
    })
    const provenance = JSON.parse(await fs.readFile(path.join(dir, "failure-parse-case", "trace.json"), "utf8")) as any
    const verification = provenance.records.find((record: any) => record.event_type === "verification")
    expect(verification.data.final_test_result).toMatchObject({
      command: "node test/pricing.test.mjs",
      status: "failed",
      exit_code: 1,
      parsed_failure_count: 1,
    })
    expect(verification.data.coverage_semantics.executed_scripts).toContain("test/pricing.test.mjs")
    expect(verification.data.coverage_semantics.covered_risks).toContain("pricing_behavior")
  })

  test("marks verification as failed when failure output is masked by shell exit code", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-masked-verification-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "masked-verification-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const stdout = [
      "file:///tmp/project/test/pricing.test.mjs:4:8",
      "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:",
      "48000 !== 51000",
      "Error: expected 51000, got 48000",
      "---EXIT: 1",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: "npm test 2>&1 || true", exit_code: 0, stdout: ${JSON.stringify(stdout)}, stderr: "" })`,
        `CaseTrace.evidenceFact({ source: "bash", category: "verification_output", summary: "Run failing tests", data: { command: "npm test 2>&1 || true", exit_code: 0, output: ${JSON.stringify(stdout)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "masked-verification-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const legacy = JSON.parse(
      await fs.readFile(path.join(dir, "masked-verification-case", "legacy-trace.json"), "utf8"),
    ) as any
    const trace = JSON.parse(await fs.readFile(path.join(dir, "masked-verification-case", "trace.json"), "utf8")) as any
    const verificationRecord = trace.records.find((record: any) => record.event_type === "verification")
    const verificationFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.fact_kind === "verification_output",
    )

    expect(legacy.verification_records[0].status).toBe("failed")
    expect(legacy.verification_records[0].quality_flags).toContain("failure_output_masked_by_exit_code")
    expect(legacy.verification_records[0].quality_flags).toContain("shell_failure_masked")
    expect(verificationRecord.status).toBe("failed")
    expect(verificationRecord.data.quality_flags).toContain("failure_output_masked_by_exit_code")
    expect(verificationRecord.data.process_exit_code).toBe(0)
    expect(verificationRecord.data.exit_masked_by_shell).toBe(true)
    expect(verificationRecord.data.parsed_command_outcomes).toContainEqual({
      source: "reported_exit_marker",
      exit_code: 1,
      status: "failed",
    })
    expect(verificationFact.data.structured_claim.value).toBe("failed")
    expect(verificationFact.data.quality_flags).toContain("failure_output_masked_by_exit_code")
  })

  test("does not infer verification failure from a source location in passing pytest output", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-passing-location-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "passing-location-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const stdout = [
      "astropy/modeling/separable.py:211: DeprecationWarning: pending cleanup",
      "...........",
      "11 passed, 1 warning in 0.08s",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: "python -m pytest astropy/modeling/tests/test_separable.py -q", exit_code: 0, stdout: ${JSON.stringify(stdout)}, stderr: "" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "passing-location-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const legacy = JSON.parse(
      await fs.readFile(path.join(dir, "passing-location-case", "legacy-trace.json"), "utf8"),
    ) as any
    const trace = JSON.parse(await fs.readFile(path.join(dir, "passing-location-case", "trace.json"), "utf8")) as any
    const verification = trace.records.find((record: any) => record.event_type === "verification")

    expect(legacy.verification_records[0].status).toBe("passed")
    expect(legacy.verification_records[0].parsed_failures).toEqual([])
    expect(legacy.verification_records[0].quality_flags).not.toContain("failure_output_masked_by_exit_code")
    expect(verification.status).toBe("passed")
    expect(verification.data.final_test_result).toMatchObject({
      status: "passed",
      exit_code: 0,
      parsed_failure_count: 0,
    })
  })

  test("tracks repository revisions and supersedes pre-change verification results", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-revision-verification-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "revision-verification-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: "npm test", exit_code: 1, stdout: "Error: expected 51000, got 48000" })`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Fix discount cap", diff: "- 0.2\\n+ 0.15" })`,
        `CaseTrace.verification({ command: "npm test", exit_code: 0, stdout: "pricing tests passed" })`,
        `CaseTrace.responseOutput({ text: "全部通过。" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "revision-verification-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "revision-verification-case", "trace.json"), "utf8"),
    ) as any
    const change = trace.records.find((record: any) => record.event_type === "change")
    const verifications = trace.records.filter((record: any) => record.event_type === "verification")

    expect(change.data.revision_before).toBe(0)
    expect(change.data.revision_after).toBe(1)
    expect(verifications[0].data.repository_revision).toBe(0)
    expect(verifications[0].data.verification_phase).toBe("baseline")
    expect(verifications[0].data.effective_for_final_state).toBe(false)
    expect(verifications[0].data.superseded_by_refs).toContain(`verification:${verifications[1].data.verification_id}`)
    expect(verifications[1].data.repository_revision).toBe(1)
    expect(verifications[1].data.verification_phase).toBe("post_change")
    expect(verifications[1].data.effective_for_final_state).toBe(true)
    expect(verifications[1].data.supersedes_refs).toContain(`verification:${verifications[0].data.verification_id}`)
  })

  test("classifies direct action and motivating evidence refs on repository changes", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-change-provenance-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "change-provenance-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "edit" })`,
        `CaseTrace.verification({ verification_id: "host_failure", command: "python -m pytest --version", exit_code: 1, status: "failed", stdout: "unsupported host python" })`,
        `CaseTrace.change({ span_id: span?.id, tool_call_id: "call_edit", files: ["src/compat.py"], intent: "Patch host compatibility", diff: "- new_api()\\n+ old_api()" })`,
        `span?.end({ output: { ok: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "change-provenance-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "change-provenance-case", "trace.json"), "utf8")) as any
    const change = trace.records.find((record: any) => record.event_type === "change")

    expect(change.source_refs).toContain("tool_call:call_edit")
    expect(change.source_refs).toContain(`span:${change.span_id}`)
    expect(change.source_refs).not.toContain("verification:host_failure")
    expect(change.data.source_ref_relations).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ source_ref: "tool_call:call_edit", relation: "materialized_by_action" }),
        expect.objectContaining({ source_ref: `span:${change.span_id}`, relation: "executed_in_span" }),
        expect.objectContaining({
          source_ref: "verification:host_failure",
          relation: "motivated_by_evidence",
          inference: "recent_failed_verification",
        }),
      ]),
    )
    expect(trace.dataflow_edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          relation: "motivated_by_evidence",
          metadata: expect.objectContaining({
            causal_semantics: "motivation_not_defect_propagation",
            evidence_tier: "temporal_advisory",
            eligible_for_attribution: false,
            derivation_method: "recent_source_fallback",
          }),
        }),
      ]),
    )
  })

  test("links failed verification to the repository changes it observed", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-failed-verification-change-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "failed-verification-change-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.change({ change_id: "shell_write", tool_call_id: "call_shell", source_refs: ["tool_call:call_shell"], files: ["src/pricing.mjs"], intent: "Shell rewrote pricing", diff: "- 0.2\\n+ 0.25" })`,
        `CaseTrace.verification({ verification_id: "failed_after_shell", tool_call_id: "call_test", command: "npm test", exit_code: 1, stdout: "Error: expected 20, got 25" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "failed-verification-change-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "failed-verification-change-case", "trace.json"), "utf8"),
    ) as any
    const verification = trace.records.find(
      (record: any) => record.event_type === "verification" && record.data.verification_id === "failed_after_shell",
    )
    expect(verification.source_refs).toContain("change:shell_write")
    expect(
      trace.dataflow_edges.some(
        (edge: any) =>
          edge.from.type === "change" &&
          edge.from.id === "shell_write" &&
          edge.to.type === "verification" &&
          edge.to.id === "failed_after_shell",
      ),
    ).toBe(true)
  })

  test("audits replayable diagnostics after repository changes without verification", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-missing-verification-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "missing-verification-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const repeated = `shared diagnostic payload:${"x".repeat(6000)}`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `await Bun.sleep(5)`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Fix discount cap", diff: "- 0.2\\n+ 0.15" })`,
        `await Bun.sleep(5)`,
        `CaseTrace.observation({ source: "tool", category: "audit", summary: "first audit observation", data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.observation({ source: "tool", category: "audit", summary: "second audit observation", data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.responseOutput({ text: "Changed src/pricing.mjs but did not run tests." })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "missing-verification-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
        OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "missing-verification-case", "trace.json"), "utf8"),
    ) as any
    const journal = (await fs.readFile(path.join(dir, "missing-verification-case", "records.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    const replayed = replayCausalIRJournal(journal)
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)
    const missingSemantic = trace.records.find(
      (record: any) =>
        record.event_type === "case.missing_semantic" && record.data.semantic_name === "final_test_result",
    )
    const observedDefect = trace.records.find(
      (record: any) =>
        record.event_type === "case.observed_defect" && record.data.defect_type === "missing_verification_after_change",
    )

    expect(
      canonicalJSONForCausalIRAudit({
        nested: { "2": "two", "10": "ten", value: [{ z: true, a: null }, , undefined] },
        omitted: undefined,
      }),
    ).toBe('{"nested":{"10":"ten","2":"two","value":[{"a":null,"z":true},null,null]}}')
    const hashMutationIndex = journal.findIndex((entry: any) => entry.operation === "node.created")
    const mappingMutationIndex = journal.findIndex((entry: any) => entry.operation === "artifact.reused")
    expect(hashMutationIndex).toBeGreaterThanOrEqual(0)
    expect(mappingMutationIndex).toBeGreaterThanOrEqual(0)
    expect(() =>
      assertCausalIRJournalAudit(
        journal.map((entry: any, index: number) =>
          index === hashMutationIndex ? { ...entry, payload_hash: "0".repeat(64) } : entry,
        ),
      ),
    ).toThrow(/payload_hash/)
    expect(() =>
      assertCausalIRJournalAudit(
        journal.map((entry: any, index: number) =>
          index === mappingMutationIndex ? { ...entry, record_type: "artifact" } : entry,
        ),
      ),
    ).toThrow(/record_type/)
    assertCausalIRJournalAudit(journal)

    expect(trace.metrics.trace_health.missing_verification_after_change).toBe(1)
    expect(issues).toContain("missing_verification_after_change")
    expect(missingSemantic.data.reason).toContain("No test-like verification command")
    expect(missingSemantic.source_refs).toEqual(expect.arrayContaining([expect.stringMatching(/^change:chg_1_/)]))
    expect(observedDefect.source_refs).toContain(`record:${missingSemantic.record_id}`)
    expect(trace.nodes.map((node: any) => node.node_id)).toEqual(
      expect.arrayContaining([missingSemantic.record_id, observedDefect.record_id]),
    )
    expect(
      journal.filter((entry: any) => entry.operation === "node.created").map((entry: any) => entry.entity_id),
    ).toEqual(expect.arrayContaining([missingSemantic.record_id, observedDefect.record_id]))
    expect(replayed.nodes.map((node: any) => node.node_id)).toEqual(
      expect.arrayContaining([missingSemantic.record_id, observedDefect.record_id]),
    )
    const journalOperations = journal.map((entry: any) => entry.operation)
    expect(journalOperations).toContain("artifact.reused")
    const reusedArtifact = journal.find((entry: any) => entry.operation === "artifact.reused")
    const createdArtifact = journal.find(
      (entry: any) => entry.operation === "artifact.created" && entry.entity_id === reusedArtifact?.entity_id,
    )
    expect(createdArtifact).toMatchObject({
      record_type: "artifact",
      data: { occurrences: 1 },
    })
    expect(createdArtifact.previous_payload_hash).toBeUndefined()
    expect(reusedArtifact).toMatchObject({
      record_type: "artifact.reuse",
      entity_id: createdArtifact?.entity_id,
      previous_payload_hash: createdArtifact?.payload_hash,
      data: { occurrences: 2 },
    })
    expect(reusedArtifact.payload_hash).toBe(causalIRPayloadHashForAudit(reusedArtifact.data))
    const diagnosticJournal = journal.filter((entry: any) =>
      [missingSemantic.record_id, observedDefect.record_id].includes(entry.entity_id),
    )
    expect(diagnosticJournal.map((entry: any) => entry.operation)).toEqual(["node.created", "node.created"])
    for (const [index, entry] of journal.entries()) {
      expect(entry).toMatchObject({
        sequence: index + 1,
        operation: expect.any(String),
        record_type: expect.any(String),
        entity_id: expect.any(String),
        payload_hash: expect.stringMatching(/^[a-f0-9]{64}$/),
      })
    }
    const graphOperations = new Set([
      "node.created",
      "node.updated",
      "edge.created",
      "artifact.created",
      "artifact.reused",
    ])
    const graphFactKeys = journal
      .filter((entry: any) => graphOperations.has(entry.operation))
      .map((entry: any) => `${entry.operation}:${entry.entity_id}:${entry.payload_hash}`)
    expect(new Set(graphFactKeys).size).toBe(graphFactKeys.length)
    expect(replayed.nodes).toEqual(trace.nodes)
    expect(replayed.edges).toEqual(trace.edges)
    expect(replayed.artifacts).toEqual(trace.artifacts)
    expect(replayed.diagnostics).toEqual(trace.diagnostics)
  })

  test("redacts special-object secrets from every output without mutating execution inputs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-sensitive-journal-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sensitive-journal-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const secrets = {
      apiKey: "sk-sensitive-api-key",
      password: "sensitive-password-value",
      token: "sensitive-environment-token",
      accessToken: "sensitive-access-token",
      plainToken: "plain-token-string-secret",
      tokenUsageString: "token-usage-string-secret",
      tokenUsageOutput: "token-usage-output-secret",
      tokenUsageExtra: "token-usage-extra-secret",
      tokenEstimateString: "token-estimate-string-secret",
      quotedJsonToken: "quoted-json-token-secret",
      quotedJsonApiKey: "quoted-json-api-key-secret",
      headerSecret: "header-credential-secret",
      shellSecret: "shell-credential-secret",
      authorizationAssignment: "authorization-assignment-secret",
      cookieSession: "cookie-session-secret",
      cookieRefresh: "cookie-refresh-secret",
      cookieError: "cookie-error-secret",
      cookieCurlSession: "cookie-curl-session-secret",
      cookieCurlRefresh: "cookie-curl-refresh-secret",
      cookieCurlSingle: "cookie-curl-single-secret",
      cookieCurlUnquoted: "cookie-curl-unquoted-secret",
      cookieCurlEmbeddedDouble: "cookie-curl-embedded-double-secret",
      cookieCurlEmbeddedSingle: "cookie-curl-embedded-single-secret",
      setCookieCurlEmbeddedDouble: "set-cookie-curl-embedded-double-secret",
      setCookieCurlEmbeddedSingle: "set-cookie-curl-embedded-single-secret",
      stringUrlPassword: "string-url-password-secret",
      stringUrlToken: "string-url-token-secret",
      errorHeaderSecret: "error-header-secret",
      errorText: "sk-error-message-secret",
      urlPassword: "url-password-plain-secret",
      urlQuery: "url-query-plain-secret",
    }

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const secrets = ${JSON.stringify(secrets)}`,
        `const error = new Error("request failed with " + secrets.errorText)`,
        `const endpoint = new URL("https://reader:" + secrets.urlPassword + "@example.com/audit?api_key=" + secrets.urlQuery + "&visible=ok")`,
        `const textSecrets = { token: secrets.plainToken, token_usage: secrets.tokenUsageString, token_estimate: secrets.tokenEstimateString, quoted_json: "{\\\"token\\\":\\\"" + secrets.quotedJsonToken + "\\\",\\\"apiKey\\\":\\\"" + secrets.quotedJsonApiKey + "\\\"}", header: "Authorization: Basic " + secrets.headerSecret + "\\nX-API-Key: " + secrets.headerSecret, cookie_header: "Cookie: session=" + secrets.cookieSession + "; refresh=" + secrets.cookieRefresh + "; Path=/", curl_header_command: "curl -sS https://example.com/audit -H \\\"Cookie: session=" + secrets.cookieCurlSession + "; refresh=" + secrets.cookieCurlRefresh + "; Path=/\\\" --compressed", quoted_header_command: "env MODE=audit curl --header='Cookie: auth=" + secrets.cookieCurlSingle + "; Path=/' https://example.com", shell: "TOKEN=" + secrets.shellSecret + " AUTHORIZATION=" + secrets.authorizationAssignment + " --password " + secrets.shellSecret, url: "https://reader:" + secrets.stringUrlPassword + "@example.com/audit?token=" + secrets.stringUrlToken, error: new Error("Cookie: session=" + secrets.cookieError + "; Path=/audit") }`,
        `textSecrets.unquoted_header_command = "env MODE=audit curl -H Cookie:auth=" + secrets.cookieCurlUnquoted + ";Path=/ --compressed https://example.com"`,
        `textSecrets.shell_fragment_double_a = 'curl -H Cookie:session="' + secrets.cookieCurlEmbeddedDouble + '" https://cookie-provenance.example/api'`,
        `textSecrets.shell_fragment_single_a = "curl -H Cookie:session='" + secrets.cookieCurlEmbeddedSingle + "' -X POST https://cookie-provenance.example/post"`,
        `textSecrets.shell_fragment_double_b = 'curl -H Set-Cookie:session="' + secrets.setCookieCurlEmbeddedDouble + '" --compressed'`,
        `textSecrets.shell_fragment_single_b = "curl -H Set-Cookie:session='" + secrets.setCookieCurlEmbeddedSingle + "' --compressed"`,
        `const environment = { apiKey: secrets.apiKey, password: secrets.password, token: secrets.token, accessToken: secrets.accessToken, error, endpoint }`,
        `const agentInput = { role: "build", error, endpoint, textSecrets }`,
        `const modelInput = { messages: ["inspect"], error, endpoint }`,
        `const unsafeTokenUsage = { input: 11, output: secrets.tokenUsageOutput, total: 18, provider_note: secrets.tokenUsageExtra, arbitrary_numeric: 42 }`,
        `const toolInput = { command: "inspect", environment, error, endpoint, token_usage: unsafeTokenUsage }`,
        `const toolOutput = { result: "ok", environment, error, endpoint, tokens: 18 }`,
        `CaseTrace.configure({ input: { task: "audit sensitive journal", agentInput, modelInput }, environment })`,
        `CaseTrace.node({ node_id: "token_usage_policy", kind: "execution.observation", component: "runtime", data: { marker: "token_usage_policy", token_usage: unsafeTokenUsage } })`,
        `CaseTrace.node({ node_id: "undefined_token_usage_policy", kind: "execution.observation", component: "runtime", data: { marker: "undefined_token_usage_policy", token_usage: undefined } })`,
        `const modelSpan = CaseTrace.get()?.startSpan({ component: "llm", operation: "generate", name: "audit-model", input: modelInput })`,
        `modelSpan?.end({ output: modelInput })`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "inspect", input: toolInput, metadata: { environment } })`,
        `span?.end({ output: toolOutput, metadata: { environment } })`,
        `const repeated = "shared sensitive audit payload:" + "x".repeat(6000)`,
        `CaseTrace.observation({ source: "tool", category: "audit", summary: "first", data: { payload: repeated } })`,
        `CaseTrace.observation({ source: "tool", category: "audit", summary: "second", data: { payload: repeated } })`,
        `CaseTrace.finish({ status: "success", result: { environment } })`,
        `process.stdout.write(JSON.stringify({ agentInput: { role: agentInput.role, error: agentInput.error.message, endpoint: agentInput.endpoint.toString(), textSecrets: { token: textSecrets.token, token_usage: textSecrets.token_usage, token_estimate: textSecrets.token_estimate, quoted_json: textSecrets.quoted_json, header: textSecrets.header, cookie_header: textSecrets.cookie_header, curl_header_command: textSecrets.curl_header_command, quoted_header_command: textSecrets.quoted_header_command, unquoted_header_command: textSecrets.unquoted_header_command, shell_fragment_double_a: textSecrets.shell_fragment_double_a, shell_fragment_single_a: textSecrets.shell_fragment_single_a, shell_fragment_double_b: textSecrets.shell_fragment_double_b, shell_fragment_single_b: textSecrets.shell_fragment_single_b, shell: textSecrets.shell, url: textSecrets.url, error: textSecrets.error.message } }, modelInput: { messages: modelInput.messages, error: modelInput.error.message, endpoint: modelInput.endpoint.toString() }, toolInput: { command: toolInput.command, error: toolInput.error.message, endpoint: toolInput.endpoint.toString(), token_usage: toolInput.token_usage }, toolOutput: { result: toolOutput.result, error: toolOutput.error.message, endpoint: toolOutput.endpoint.toString(), tokens: toolOutput.tokens }, environment: { apiKey: environment.apiKey, password: environment.password, token: environment.token, accessToken: environment.accessToken, error: environment.error.message, endpoint: environment.endpoint.toString() } }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "sensitive-journal-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
        OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const originals = JSON.parse(await new Response(proc.stdout).text())
    const originalError = `request failed with ${secrets.errorText}`
    const originalEndpoint = `https://reader:${secrets.urlPassword}@example.com/audit?api_key=${secrets.urlQuery}&visible=ok`
    expect(originals.environment).toEqual({
      apiKey: secrets.apiKey,
      password: secrets.password,
      token: secrets.token,
      accessToken: secrets.accessToken,
      error: originalError,
      endpoint: originalEndpoint,
    })
    expect(originals.agentInput).toEqual({
      role: "build",
      error: originalError,
      endpoint: originalEndpoint,
      textSecrets: {
        token: secrets.plainToken,
        token_usage: secrets.tokenUsageString,
        token_estimate: secrets.tokenEstimateString,
        quoted_json: `{"token":"${secrets.quotedJsonToken}","apiKey":"${secrets.quotedJsonApiKey}"}`,
        header: `Authorization: Basic ${secrets.headerSecret}\nX-API-Key: ${secrets.headerSecret}`,
        cookie_header: `Cookie: session=${secrets.cookieSession}; refresh=${secrets.cookieRefresh}; Path=/`,
        curl_header_command: `curl -sS https://example.com/audit -H "Cookie: session=${secrets.cookieCurlSession}; refresh=${secrets.cookieCurlRefresh}; Path=/" --compressed`,
        quoted_header_command: `env MODE=audit curl --header='Cookie: auth=${secrets.cookieCurlSingle}; Path=/' https://example.com`,
        unquoted_header_command: `env MODE=audit curl -H Cookie:auth=${secrets.cookieCurlUnquoted};Path=/ --compressed https://example.com`,
        shell_fragment_double_a: `curl -H Cookie:session="${secrets.cookieCurlEmbeddedDouble}" https://cookie-provenance.example/api`,
        shell_fragment_single_a: `curl -H Cookie:session='${secrets.cookieCurlEmbeddedSingle}' -X POST https://cookie-provenance.example/post`,
        shell_fragment_double_b: `curl -H Set-Cookie:session="${secrets.setCookieCurlEmbeddedDouble}" --compressed`,
        shell_fragment_single_b: `curl -H Set-Cookie:session='${secrets.setCookieCurlEmbeddedSingle}' --compressed`,
        shell: `TOKEN=${secrets.shellSecret} AUTHORIZATION=${secrets.authorizationAssignment} --password ${secrets.shellSecret}`,
        url: `https://reader:${secrets.stringUrlPassword}@example.com/audit?token=${secrets.stringUrlToken}`,
        error: `Cookie: session=${secrets.cookieError}; Path=/audit`,
      },
    })
    expect(originals.modelInput).toEqual({ messages: ["inspect"], error: originalError, endpoint: originalEndpoint })
    expect(originals.toolInput).toEqual({
      command: "inspect",
      error: originalError,
      endpoint: originalEndpoint,
      token_usage: {
        input: 11,
        output: secrets.tokenUsageOutput,
        total: 18,
        provider_note: secrets.tokenUsageExtra,
        arbitrary_numeric: 42,
      },
    })
    expect(originals.toolOutput).toEqual({ result: "ok", error: originalError, endpoint: originalEndpoint, tokens: 18 })
    expect(originals.toolInput.token_usage).toEqual({
      input: 11,
      output: secrets.tokenUsageOutput,
      total: 18,
      provider_note: secrets.tokenUsageExtra,
      arbitrary_numeric: 42,
    })
    expect(originals.toolOutput.tokens).toBe(18)

    const caseDir = path.join(dir, "sensitive-journal-case")
    const recordsText = await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")
    const journal = recordsText
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const tokenUsagePolicy = trace.nodes.find((item: any) => item.node_id === "token_usage_policy")
    const undefinedTokenUsagePolicy = trace.nodes.find((item: any) => item.node_id === "undefined_token_usage_policy")
    const artifactDir = path.join(caseDir, "artifacts", "sha256")
    const artifactFiles = await fs.readdir(artifactDir)
    const persistedText = await Promise.all(
      [
        "events.jsonl",
        "raw-events.jsonl",
        "records.jsonl",
        "trace.json",
        "legacy-trace.json",
        "provenance-trace.json",
        "partial/latest.json",
        "manifest.json",
        "trace.html",
      ]
        .map((file) => path.join(caseDir, file))
        .concat(artifactFiles.map((file) => path.join(artifactDir, file)))
        .map((file) => fs.readFile(file, "utf8")),
    )

    expect(journal.map((entry: any) => entry.operation)).toEqual(
      expect.arrayContaining(["node.created", "node.updated", "artifact.created", "artifact.reused", "case.finalized"]),
    )
    assertCausalIRJournalAudit(journal)
    const replayed = replayCausalIRJournal(journal)
    expect(replayed.nodes).toEqual(trace.nodes)
    expect(replayed.edges).toEqual(trace.edges)
    expect(replayed.artifacts).toEqual(trace.artifacts)
    expect(replayed.diagnostics).toEqual(trace.diagnostics)
    expect(tokenUsagePolicy.payload.token_usage).toEqual({ input: 11, total: 18 })
    expect(undefinedTokenUsagePolicy.payload).not.toHaveProperty("token_usage")
    const leakedSecrets = Object.values(secrets).filter((secret) => persistedText.some((text) => text.includes(secret)))
    expect(leakedSecrets).toEqual([])
    expect(persistedText.some((text) => text.includes("https://cookie-provenance.example/api"))).toBe(true)
    expect(persistedText.some((text) => text.includes("-X POST https://cookie-provenance.example/post"))).toBe(true)
    expect(recordsText).toContain('"apiKey":"[REDACTED]"')
    expect(recordsText).toContain('"password":"[REDACTED]"')
    expect(recordsText).toContain('"token":"[REDACTED]"')
    expect(recordsText).toContain('"accessToken":"[REDACTED]"')
  })

  test("omits root non-object token_usage while preserving the closed numeric schema", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-root-token-usage-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "root-token-usage.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.node({ node_id: "string_usage", kind: "execution.observation", component: "runtime", data: { marker: "string_usage", token_usage: "not-a-token-usage-object" } })`,
        `CaseTrace.node({ node_id: "undefined_usage", kind: "execution.observation", component: "runtime", data: { marker: "undefined_usage", token_usage: undefined } })`,
        `CaseTrace.node({ node_id: "numeric_usage", kind: "execution.observation", component: "runtime", data: { marker: "numeric_usage", token_usage: { input: 4, output: 5, total: 9, provider_note: "omit-me", arbitrary_numeric: 42 } } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "root-token-usage-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "root-token-usage-case", "trace.json"), "utf8")) as any
    const payload = (nodeID: string) => trace.nodes.find((item: any) => item.node_id === nodeID).payload
    expect(payload("string_usage")).not.toHaveProperty("token_usage")
    expect(payload("undefined_usage")).not.toHaveProperty("token_usage")
    expect(payload("numeric_usage").token_usage).toEqual({ input: 4, output: 5, total: 9 })
  })

  test("redacts direct summarizeText special objects across persisted trace outputs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-summarize-special-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "summarize-special-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const secrets = {
      urlPassword: "summarize-url-password-secret",
      apiKey: "summarize-api-key-secret",
      password: "summarize-error-password-secret",
    }
    const ordinaryText = "ordinary summary text ".repeat(5)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace, summarizeText } from ${JSON.stringify(traceModule)}`,
        `const secrets = ${JSON.stringify(secrets)}`,
        `const endpoint = new URL("https://reader:" + secrets.urlPassword + "@example.com/audit?api_key=" + secrets.apiKey + "&visible=ok")`,
        `const error = new Error("request failed: password=" + secrets.password + " " + "x".repeat(96))`,
        `const ordinaryText = ${JSON.stringify(ordinaryText)}`,
        `CaseTrace.configure({ input: { task: "summarize special objects" } })`,
        `const urlSummary = CaseTrace.summarizeText(endpoint)`,
        `const moduleErrorSummary = summarizeText(error)`,
        `const traceErrorSummary = CaseTrace.summarizeText(error)`,
        `const ordinarySummary = CaseTrace.summarizeText(ordinaryText)`,
        `CaseTrace.node({ node_id: "direct_summaries", kind: "verification", component: "tool", title: "direct summaries", data: { urlSummary, moduleErrorSummary, traceErrorSummary, ordinarySummary } })`,
        `CaseTrace.finish({ status: "success" })`,
        `process.stdout.write(JSON.stringify({ endpoint: endpoint.toString(), error: error.message }))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "summarize-special-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
        OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    expect(JSON.parse(await new Response(proc.stdout).text())).toEqual({
      endpoint: `https://reader:${secrets.urlPassword}@example.com/audit?api_key=${secrets.apiKey}&visible=ok`,
      error: `request failed: password=${secrets.password} ${"x".repeat(96)}`,
    })

    const caseDir = path.join(dir, "summarize-special-case")
    const recordsText = await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")
    const journal = recordsText
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const summaries = trace.nodes.find((node: any) => node.node_id === "direct_summaries").data
    const artifactDir = path.join(caseDir, "artifacts", "sha256")
    const artifactFiles = await fs.readdir(artifactDir)
    const persistedText = await Promise.all(
      [
        "events.jsonl",
        "raw-events.jsonl",
        "records.jsonl",
        "trace.json",
        "legacy-trace.json",
        "provenance-trace.json",
        "partial/latest.json",
        "manifest.json",
        "trace.html",
      ]
        .map((file) => path.join(caseDir, file))
        .concat(artifactFiles.map((file) => path.join(artifactDir, file)))
        .map((file) => fs.readFile(file, "utf8")),
    )

    expect(summaries.urlSummary.artifact_id).toBeTruthy()
    expect(summaries.traceErrorSummary.artifact_id).toBeTruthy()
    expect(summaries.ordinarySummary).toMatchObject({
      length: ordinaryText.length,
      preview: ordinaryText.slice(0, 64),
    })
    expect(summaries.ordinarySummary.artifact_id).toBeTruthy()
    const ordinaryArtifact = trace.artifacts.find(
      (artifact: any) => artifact.artifact_id === summaries.ordinarySummary.artifact_id,
    )
    expect(await fs.readFile(path.join(caseDir, ordinaryArtifact.path), "utf8")).toBe(ordinaryText)
    assertCausalIRJournalAudit(journal)
    const replayed = replayCausalIRJournal(journal)
    expect(replayed.nodes).toEqual(trace.nodes)
    expect(replayed.edges).toEqual(trace.edges)
    expect(replayed.artifacts).toEqual(trace.artifacts)
    expect(replayed.diagnostics).toEqual(trace.diagnostics)
    for (const secret of Object.values(secrets)) {
      expect(persistedText.every((text) => !text.includes(secret))).toBe(true)
    }
  })

  test("does not register or render an artifact until its atomic write succeeds", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-write-failure-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "artifact-write-failure.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ input: { task: "artifact failure remains passive" } })`,
        `if (trace) fs.writeFileSync(trace.artifactDir, "artifact directory blocker")`,
        `CaseTrace.observation({ source: "tool", category: "artifact_failure", summary: "large payload", data: { payload: "x".repeat(5000) }, source_refs: [] })`,
        `CaseTrace.finish({ status: "success", result: { answer: "agent result preserved" } })`,
        `process.stdout.write("agent result preserved")`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "artifact-write-failure-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(await new Response(proc.stdout).text()).toBe("agent result preserved")

    const caseDir = path.join(dir, "artifact-write-failure-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const observation = trace.records.find((item: any) => item.title === "artifact_failure")
    const payloadSummary = observation.data.data

    expect(trace.artifacts).toEqual([])
    expect(payloadSummary.artifact_id).toBeUndefined()
    expect(payloadSummary.artifact_status).toBe("write_failed")
    expect(trace.diagnostics).toContainEqual(
      expect.objectContaining({ kind: "artifact_write_failed", status: "write_failed" }),
    )
    expect(html).not.toContain('href="artifacts/sha256/')
    expect((await readCausalIRJournal(caseDir)).some((entry: any) => entry.operation === "artifact.created")).toBe(
      false,
    )
  })

  test("updates and removes current formal diagnostics without duplicate journal facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-diagnostic-reconcile-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "diagnostic-reconcile-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `await Bun.sleep(5)`,
        `CaseTrace.change({ files: ["src/first.mjs"], intent: "first change", diff: "- 1\\n+ 2" })`,
        `await Bun.sleep(5)`,
        `CaseTrace.change({ files: ["src/second.mjs"], intent: "second change", diff: "- 3\\n+ 4" })`,
        `await Bun.sleep(5)`,
        `CaseTrace.responseOutput({ text: "Changes are pending verification." })`,
        `await Bun.sleep(5)`,
        `CaseTrace.verification({ command: "bun test", exit_code: 0, status: "passed", stdout: "tests passed" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "diagnostic-reconcile-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "diagnostic-reconcile-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)
    const diagnosticIDs = ["missing_semantic_final_test_result", "observed_defect_missing_verification_after_change"]

    assertCausalIRJournalAudit(journal)
    const checkpoints = journal.filter((entry: any) => entry.operation === "case.checkpointed")
    const finalized = journal.find((entry: any) => entry.operation === "case.finalized")
    expect(checkpoints.length).toBeGreaterThan(0)
    expect(
      checkpoints.every(
        (entry: any) => entry.record_type === "checkpoint" && entry.entity_id === trace.manifest.case_id,
      ),
    ).toBe(true)
    expect(finalized).toMatchObject({
      record_type: "finish",
      entity_id: trace.manifest.case_id,
      previous_payload_hash: checkpoints.at(-1)?.payload_hash,
    })
    assertExactlyOneFinalizationAtEnd(journal)

    for (const diagnosticID of diagnosticIDs) {
      const entries = journal.filter((entry: any) => entry.entity_id === diagnosticID)
      expect(entries.map((entry: any) => entry.operation)).toEqual(["node.created", "node.updated"])
      expect(new Set(entries.map((entry: any) => entry.payload_hash)).size).toBe(entries.length)
    }
    expect(journal.some((entry: any) => entry.operation === "case.checkpointed")).toBe(true)
    expect(trace.records.some((record: any) => diagnosticIDs.includes(record.record_id))).toBe(false)
    expect(trace.nodes.some((node: any) => diagnosticIDs.includes(node.node_id))).toBe(false)
    assertJournalReplaysCanonicalTrace(journal, trace)
  })

  test("removes completion diagnostics when a signal interrupts the case before completion", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-interrupted-diagnostics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "interrupted-diagnostics-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "change then verify" }, environment: { model: "unit-test" } })`,
        `CaseTrace.change({ files: ["src/changed.mjs"], intent: "change awaiting verification", diff: "- 1\\n+ 2" })`,
        `await Bun.sleep(25)`,
        `CaseTrace.finish({ status: "cancelled", result: { reason: "SIGTERM", signal: "SIGTERM", trace_html_flush: "process_signal" } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "interrupted-diagnostics-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "interrupted-diagnostics-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)
    const diagnosticIDs = ["missing_semantic_final_test_result", "observed_defect_missing_verification_after_change"]
    const failed = trace.records.find((record: any) => record.event_type === "case.failed")
    const signal = trace.records.find((record: any) => record.event_type === "process.signal")

    expect(trace.manifest.shutdown_disposition).toBe("interrupted_before_case_completion")
    expect(failed).toBeTruthy()
    expect(signal).toBeTruthy()
    expect(signal.data).toMatchObject({
      signal: "SIGTERM",
      shutdown_disposition: "interrupted_before_case_completion",
      sender_identity_available: false,
    })
    expect(failed.source_refs).toContain(`node:${signal.record_id}`)
    expect(
      trace.edges.some(
        (edge: any) =>
          edge.from?.ref_id === signal.record_id &&
          edge.to?.ref_id === failed.record_id &&
          edge.normalized_relation === "failed_before" &&
          edge.eligible_for_attribution === true,
      ),
    ).toBe(true)
    expect(journal.some((entry: any) => diagnosticIDs.includes(entry.entity_id))).toBe(true)
    expect(trace.records.some((record: any) => diagnosticIDs.includes(record.record_id))).toBe(false)
    expect(trace.nodes.some((node: any) => diagnosticIDs.includes(node.node_id))).toBe(false)
    assertJournalReplaysCanonicalTrace(journal, trace)
  })

  test("records passive verification-attempt semantics for handwritten assertion scripts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-handwritten-verification-attempt-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "handwritten-verification-attempt.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const command = `python3 -c "assert 2 + 2 == 4; assert 'owner'.upper() == 'OWNER'; print('ALL TESTS PASSED')"`

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: ${JSON.stringify(command)}, purpose: "Run handwritten full integration test", exit_code: 0, status: "passed", stdout: "ALL TESTS PASSED\\n2 assertions" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "handwritten-verification-attempt-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "handwritten-verification-attempt-case", "trace.json"), "utf8"),
    ) as any
    const verification = trace.records.find((record: any) => record.event_type === "verification")

    expect(verification.data.final_test_result.result_kind).toBe("test_result")
    expect(verification.data.verification_attempt).toMatchObject({
      attempt_kind: "handwritten_assertion_script",
      detection_method: "passive_command_analysis",
      assertion_count: 2,
      declared_scope: "full_integration",
      oracle_source: "inline_assertions",
      behavior_impact: "none",
    })
    expect(verification.data.quality_flags).toContain("handwritten_self_test")
  })

  test("marks verification risk when test oracles were changed before tests passed", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-test-oracle-risk-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "test-oracle-risk-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const productionDiff = [
      "Index: src/pricing.mjs",
      "@@",
      "-  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)",
      "+  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)",
    ].join("\n")
    const productionFile = path.join(dir, "test-oracle-trap", "src", "pricing.mjs")
    const testFile = path.join(dir, "test-oracle-trap", "test", "pricing.test.mjs")
    const testDiff = [
      "Index: test/pricing.test.mjs",
      "@@",
      "-assert.equal(renewalQuote({ baseCents: 1200, seats: 50, loyaltyYears: 5 }), 51000)",
      "+assert.equal(renewalQuote({ baseCents: 1200, seats: 50, loyaltyYears: 5 }), 48000)",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const prod = CaseTrace.change({ files: [${JSON.stringify(productionFile)}], intent: "change production cap", diff: ${JSON.stringify(productionDiff)} })`,
        `const test = CaseTrace.change({ files: [${JSON.stringify(testFile)}], intent: "update pricing assertion", diff: ${JSON.stringify(testDiff)} })`,
        `const verification = CaseTrace.verification({ command: "npm test", exit_code: 0, stdout: "pricing tests passed", status: "passed" })`,
        `CaseTrace.responseOutput({ text: "npm test passed after updating pricing.", source_refs: verification ? ["verification:" + verification.verification_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "test-oracle-risk-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "test-oracle-risk-case", "trace.json"), "utf8")) as any
    const changes = trace.records.filter((record: any) => record.event_type === "change")
    const verification = trace.records.find((record: any) => record.event_type === "verification")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)

    const productionChange = changes.find((record: any) => record.data.files.includes(productionFile))
    expect(productionChange.data.change_target_role).toBe("production_code")
    const testChange = changes.find((record: any) => record.data.files.includes(testFile))
    expect(testChange.data.change_target_role).toBe("test_code")
    expect(testChange.data.change_semantics.risk_flags).toContain("test_oracle_changed")
    expect(verification.data.changed_test_refs).toContain(`change:${testChange.data.change_id}`)
    expect(verification.data.changed_production_refs).toContain(`change:${productionChange.data.change_id}`)
    expect(verification.data.verification_scope_risk_flags).toContain("tests_modified_before_verification")
    expect(verification.data.verification_scope_risk_flags).toContain("test_oracle_modified_before_verification")
    expect(assessment.data.verification_after_test_change_refs).toContain(
      `verification:${verification.data.verification_id}`,
    )
    expect(trace.metrics.trace_health.verification_after_test_change).toBe(1)
    expect(issues).toContain("verification_after_test_change")
  })

  test("derives structured semantic facts from repository change diffs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-change-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "change-semantics-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const capDiff = [
      "Index: src/pricing.mjs",
      "@@",
      "-  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)",
      "+  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)",
    ].join("\n")
    const hardcodeDiff = [
      "@@",
      "-  return Math.round(base * seats * (1 - discount))",
      "+  if (input.baseCents === 1200 && input.seats === 50) return 51000",
      "+  return Math.round(base * seats * (1 - discount))",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Fix discount cap", diff: ${JSON.stringify(capDiff)} })`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Special-case failing pricing test", diff: ${JSON.stringify(hardcodeDiff)} })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "change-semantics-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const legacy = JSON.parse(
      await fs.readFile(path.join(dir, "change-semantics-case", "legacy-trace.json"), "utf8"),
    ) as any
    const trace = JSON.parse(await fs.readFile(path.join(dir, "change-semantics-case", "trace.json"), "utf8")) as any
    const changeRecords = trace.records.filter((record: any) => record.event_type === "change")

    expect(legacy.change_records[0].change_semantics.numeric_constant_changes).toContainEqual(
      expect.objectContaining({ from: "0.2", to: "0.15" }),
    )
    expect(changeRecords[0].data.change_semantics.operation_kinds).toContain("numeric_constant_update")
    expect(changeRecords[0].data.change_semantics.risk_flags).toContain("numeric_constant_changed")
    expect(changeRecords[0].data.change_semantics.changed_identifiers).toContain("discount")
    expect(changeRecords[0].data.diff_semantics.numeric_constant_changes).toContainEqual(
      expect.objectContaining({ from: "0.2", to: "0.15" }),
    )
    expect(changeRecords[0].data.diff_semantics.changed_symbols).toContain("discount")
    expect(changeRecords[0].data.diff_semantics.semantic_summary).toContain("numeric_constant_update")
    expect(changeRecords[1].data.change_semantics.operation_kinds).toContain("conditional_logic_change")
    expect(changeRecords[1].data.change_semantics.risk_flags).toContain("hardcode_candidate")
    expect(changeRecords[1].data.diff_semantics.risk_flags).toContain("hardcode_candidate")
  })

  test("annotates semantic fact conflicts and active versus legacy applicability", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-fact-conflicts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "fact-conflicts-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const active = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "docs/architecture.md says the active renewal discount cap must be 15 percent", data: { subject: "renewalQuote", predicate: "discount_cap", value: "15 percent", path: "docs/architecture.md", line_start: 7, line_end: 7, output: "The active renewal discount cap must be 15 percent." } })`,
        `const legacy = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "legacy implementation still uses 20 percent cap", data: { subject: "renewalQuote", predicate: "discount_cap", value: "20 percent", path: "docs/architecture.md", line_start: 10, line_end: 10, output: "Legacy implementation retained for migration comparison still uses a 20 percent cap." } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "loyalty discount is 10 percent, not cap", data: { subject: "discount", predicate: "discount_cap", value: "10 percent", path: "docs/architecture.md", line_start: 11, line_end: 11, output: "Loyalty discount: 10 percent when loyaltyYears >= 3." } })`,
        `CaseTrace.responseOutput({ text: "renewalQuote discount cap is 20 percent.", source_refs: legacy ? ["evidence:" + legacy.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "fact-conflicts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "fact-conflicts-case", "trace.json"), "utf8")) as any
    const activeFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.structured_claim?.value === "15 percent",
    )
    const legacyFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.structured_claim?.value === "20 percent",
    )
    const loyaltyFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" && record.data.structured_claim?.value === "10 percent",
    )
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const assessment = trace.records.find((record: any) => record.event_type === "claim.support_assessment")
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)

    expect(activeFact.data.applicability_status).toBe("active")
    expect(legacyFact.data.applicability_status).toBe("legacy")
    expect(loyaltyFact.data.conflict_group_id).toBeUndefined()
    expect(activeFact.data.conflict_group_id).toBeTruthy()
    expect(legacyFact.data.conflict_group_id).toBe(activeFact.data.conflict_group_id)
    expect(claim.data.conflicting_evidence_refs).toContain(`evidence:${activeFact.record_id}`)
    expect(claim.data.support_conflict_status).toBe("conflicted")
    expect(assessment.data.conflicting_evidence_refs).toContain(`evidence:${activeFact.record_id}`)
    expect(trace.metrics.trace_health.conflicting_semantic_fact_groups).toBe(1)
    expect(trace.metrics.trace_health.legacy_fact_used_in_final_claim).toBe(1)
    expect(issues).toContain("conflicting_semantic_fact_group")
    expect(issues).toContain("legacy_fact_used_in_final_claim")
  })

  test("classifies semantic conflict roles for attribution", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-fact-roles-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "fact-role-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "architecture says current discount cap must be 15 percent", data: { subject: "discount", predicate: "discount_cap", value: "15 percent", path: "/tmp/active-legacy-conflict/docs/architecture.md", line_start: 7, line_end: 7, snippet_preview: "The current renewal discount cap must be 15 percent.", output: "Line 5: Legacy implementation is retained only for migration comparison.\\nLine 7: The current renewal discount cap must be 15 percent." } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "active pricing code caps discount at 20 percent before edit", data: { subject: "discount", predicate: "discount_cap", value: "0.2", path: "src/pricing.mjs", line_start: 6, line_end: 6, output: "const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)" } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "legacy implementation retained only for migration comparison uses 20 percent cap", data: { subject: "discount", predicate: "discount_cap", value: "20 percent", path: "src/legacy/pricing.mjs", line_start: 6, line_end: 6, output: "Legacy implementation retained only for migration comparison uses a 20 percent cap." } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "stale pricing test expects 20 percent cap", data: { subject: "discount", predicate: "discount_cap", value: "20 percent", path: "test/pricing.test.mjs", line_start: 4, line_end: 4, output: "assert.equal(renewalQuote({ baseCents: 1200, seats: 50, loyaltyYears: 4 }), 48000)" } })`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Fix current implementation cap", diff: "- const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)\\\\n+ const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)" })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "active pricing code caps discount at 15 percent after edit", data: { subject: "discount", predicate: "discount_cap", value: "0.15", path: "src/pricing.mjs", line_start: 6, line_end: 6, output: "const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "fact-role-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "fact-role-case", "trace.json"), "utf8")) as any
    const facts = trace.records.filter((record: any) => record.event_type === "evidence.semantic_fact")
    const byValue = (value: string) => facts.find((record: any) => record.data.structured_claim?.value === value)
    const requirementFact = byValue("15 percent")
    const preChangeCodeFact = byValue("0.2")
    const legacyFact = facts.find(
      (record: any) =>
        record.data.structured_claim?.value === "20 percent" &&
        record.data.structured_claim?.source_span?.path === "src/legacy/pricing.mjs",
    )
    const testFact = facts.find(
      (record: any) =>
        record.data.structured_claim?.value === "20 percent" &&
        record.data.structured_claim?.source_span?.path === "test/pricing.test.mjs",
    )
    const postChangeCodeFact = byValue("0.15")
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)

    expect(requirementFact.data.semantic_role).toBe("requirement_rule")
    expect(preChangeCodeFact.data.semantic_role).toBe("observed_pre_change_code")
    expect(legacyFact.data.semantic_role).toBe("legacy_historical")
    expect(testFact.data.semantic_role).toBe("test_expectation")
    expect(postChangeCodeFact.data.semantic_role).toBe("observed_post_change_code")
    expect(requirementFact.data.conflict_kind).toBe("requirement_code_mismatch")
    expect(preChangeCodeFact.data.conflict_severity).toBe("high")
    expect(preChangeCodeFact.data.conflict_issue).toBe(true)
    expect(legacyFact.data.conflict_issue).toBe(false)
    expect(trace.metrics.trace_health.actionable_semantic_conflict_groups).toBe(1)
    expect(issues).toContain("requirement_code_mismatch")
  })

  test("supports unchanged path claims with change-scope exclusion facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-unchanged-path-claim-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "unchanged-path-claim.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const change = CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Fix current pricing cap", diff: "- 0.2\\\\n+ 0.15" })`,
        `CaseTrace.responseOutput({ text: "Left src/legacy/ untouched.", source_refs: change ? ["change:" + change.change_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "unchanged-path-claim-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "unchanged-path-claim-case", "trace.json"), "utf8"),
    ) as any
    const exclusionFact = trace.records.find(
      (record: any) =>
        record.event_type === "evidence.semantic_fact" &&
        record.data.structured_claim?.predicate === "change_scope_excludes" &&
        record.data.structured_claim?.value === "src/legacy/",
    )
    const unchangedClaim = trace.records.find(
      (record: any) =>
        record.event_type === "response.claim" &&
        String(record.data.text.preview ?? record.data.text).includes("src/legacy/ untouched"),
    )

    expect(exclusionFact).toBeTruthy()
    expect(exclusionFact.source_refs).toContain(`change:${changeIdFromTrace(trace)}`)
    expect(unchangedClaim.data.direct_evidence_refs).toContain(`evidence:${exclusionFact.record_id}`)
    expect(unchangedClaim.data.quality_flags).not.toContain("context_only_claim")
    expect(unchangedClaim.data.quality_flags).not.toContain("execution_only_claim")
  })

  test("does not flag legacy evidence noise when final claim is directly supported by current evidence", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-legacy-noise-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "legacy-noise-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const current = CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "current architecture says discount cap is 15 percent", data: { subject: "discount", predicate: "discount_cap", value: "15 percent", path: "docs/architecture.md", line_start: 7, line_end: 7, output: "The current renewal discount cap must be 15 percent." } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "file_read", summary: "legacy implementation retained only for migration comparison uses 20 percent", data: { subject: "discount", predicate: "discount_cap", value: "20 percent", path: "src/legacy/pricing.mjs", line_start: 6, line_end: 6, output: "Legacy implementation retained only for migration comparison uses a 20 percent cap." } })`,
        `CaseTrace.responseOutput({ text: "The current discount cap is 15 percent.", source_refs: current ? ["evidence:" + current.node_id, "context:legacy_scan"] : ["context:legacy_scan"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "legacy-noise-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "legacy-noise-case", "trace.json"), "utf8")) as any
    const claim = trace.records.find((record: any) => record.event_type === "response.claim")
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)

    expect(claim.data.direct_evidence_refs.length).toBeGreaterThan(0)
    expect(claim.data.quality_flags).not.toContain("legacy_evidence_used")
    expect(trace.metrics.trace_health.legacy_fact_used_in_final_claim).toBe(0)
    expect(issues).not.toContain("legacy_fact_used_in_final_claim")
  })

  test("emits task obligations from the prompt and evaluates unmet actions", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-obligations-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "obligation-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "必须调用 syntheticFacts.repo_fact 获取事实。只能修改 src/billing，不允许修改 src/payment。最后运行 npm test。" } })`,
        `CaseTrace.change({ files: ["src/billing/pricing.mjs"], intent: "Fix billing pricing cap", diff: "- 0.2\\\\n+ 0.15" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "obligation-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "obligation-case", "trace.json"), "utf8")) as any
    const obligations = trace.records.filter((record: any) => record.event_type === "task.obligation")
    const byType = (type: string) => obligations.find((record: any) => record.data.obligation_type === type)
    const issues = trace.metrics.trace_health.issues.map((issue: any) => issue.kind)

    expect(byType("verification_required").data.status).toBe("unmet")
    expect(byType("mcp_required").data.status).toBe("unmet")
    expect(byType("path_scope_exclusion").data.status).toBe("fulfilled")
    expect(byType("path_scope_exclusion").data.target_path).toBe("src/payment")
    expect(trace.metrics.trace_health.task_obligations).toBeGreaterThanOrEqual(3)
    expect(trace.metrics.trace_health.unmet_task_obligations).toBe(2)
    expect(issues).toContain("task_obligation_unmet")
  })

  test("emits task obligations from prompt assembly records on HTTP session path", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-obligation-prompt-assembly-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "obligation-prompt-assembly-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.promptAssembly({ stage: "initial_user_request", session_id: "ses_unit", input: { parts: [{ type: "text", text: "请必须调用 syntheticFacts.repo_fact 获取 discount-policy，再修复 renewalQuote。最终说明 MCP 返回的事实、修改点和 npm test 结果。" }] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "obligation-prompt-assembly-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(
      await fs.readFile(path.join(dir, "obligation-prompt-assembly-case", "trace.json"), "utf8"),
    ) as any
    const prompt = trace.records.find((record: any) => record.event_type === "prompt.assembly")
    const obligations = trace.records.filter((record: any) => record.event_type === "task.obligation")
    const mcp = obligations.find((record: any) => record.data.obligation_type === "mcp_required")
    const verification = obligations.find((record: any) => record.data.obligation_type === "verification_required")

    expect(prompt).toBeTruthy()
    expect(mcp.data.status).toBe("unmet")
    expect(mcp.data.requirement_source_refs).toContain(`prompt:${prompt.record_id}`)
    expect(mcp.source_refs).toContain(`prompt:${prompt.record_id}`)
    expect(verification.data.status).toBe("unmet")
    expect(trace.metrics.trace_health.task_obligations).toBeGreaterThanOrEqual(2)
  })

  test("does not create response claims from English design section headings", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-design-heading-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "design-heading-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "Design constraints honored:\\n- General logic remains unchanged." })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "design-heading-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "design-heading-case", "trace.json"), "utf8")) as any
    const claims = trace.records.filter((record: any) => record.event_type === "response.claim")

    expect(claims.map((record: any) => record.data.text.preview ?? record.data.text)).not.toContain(
      "Design constraints honored:",
    )
  })

  test("evaluates read-only constraints at trace finish", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-constraint-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "constraint-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ caseID: "readonly-ok" })`,
        `CaseTrace.constraint({ source: "user", constraint: "Do not modify repository files", status: "unknown" })`,
        `CaseTrace.finish({ status: "success" })`,
        `CaseTrace.configure({ caseID: "readonly-violated" })`,
        `CaseTrace.constraint({ source: "user", constraint: "Do not modify repository files", status: "unknown" })`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "unexpected edit" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const ok = JSON.parse(await fs.readFile(path.join(dir, "readonly-ok", "legacy-trace.json"), "utf8")) as any
    const violated = JSON.parse(
      await fs.readFile(path.join(dir, "readonly-violated", "legacy-trace.json"), "utf8"),
    ) as any

    expect(ok.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_satisfied",
    })
    expect(violated.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_violated",
    })
  })

  test("uses concrete source refs for response output segments", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-source-refs-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "evidence-ref-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "bash" })`,
        `const ctx = CaseTrace.contextSnapshot({ phase: "llm_request", messages: [{ role: "user", content: "fix tests" }] })`,
        `const ver = CaseTrace.verification({ span_id: span?.id, command: "node test/pricing.test.mjs", exit_code: 1, stdout: "Error: expected 170, got 30" })`,
        `const chg = CaseTrace.change({ span_id: span?.id, files: ["src/pricing.mjs"], intent: "Fix discount formula" })`,
        `span?.end({ output: { ok: true } })`,
        `CaseTrace.responseOutput({ text: "Fixed discount calculation.", source_refs: ["recent_tool_results", "recent_verification_records", "recent_change_records"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "source-ref-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const legacy = JSON.parse(await fs.readFile(path.join(dir, "source-ref-case", "legacy-trace.json"), "utf8")) as any
    const trace = JSON.parse(await fs.readFile(path.join(dir, "source-ref-case", "trace.json"), "utf8")) as any
    const refs = legacy.response_segments[0].source_refs ?? []
    const response = trace.records.find((record: any) => record.event_type === "response.output")
    const advisoryEdges = temporalAdvisoryEdgesTo(trace, response.record_id)
    const advisoryRefs = temporalAdvisoryMemberRefs(trace, response.record_id)

    expect(refs).toEqual([])
    expect(advisoryRefs).toContain(`context_snapshot:${legacy.context_snapshots[0].snapshot_id}`)
    expect(advisoryRefs).toContain(`tool_span:${legacy.spans[0].span_id}`)
    expect(advisoryRefs).toContain(`verification:${legacy.verification_records[0].verification_id}`)
    expect(advisoryRefs).toContain(`change:${legacy.change_records[0].change_id}`)
    expect(advisoryEdges.every((edge: any) => edge.eligible_for_attribution === false)).toBe(true)
  })

  test("does not let trace-only controls alter compaction behavior", async () => {
    const packageDir = path.resolve(import.meta.dir, "../..")
    const checkedText = (
      await Promise.all([
        fs.readFile(path.join(packageDir, "src/session/prompt.ts"), "utf8"),
        fs.readFile(path.join(packageDir, "test/observability/stress-cases/cases.json"), "utf8"),
      ])
    ).join("\n")

    expect(checkedText).not.toContain("OPENCODE_TRACE_FORCE_COMPACTION")
    expect(checkedText).not.toContain("forced_trace_compaction")
    expect(checkedText).not.toContain("deterministic_forced_compaction")
  })

  test("declares passive sidecar collection with no behavior impact", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-passive-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "passive-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "passive-sidecar-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const manifest = JSON.parse(
      await fs.readFile(path.join(dir, "passive-sidecar-case", "manifest.json"), "utf8"),
    ) as any
    const trace = JSON.parse(await fs.readFile(path.join(dir, "passive-sidecar-case", "trace.json"), "utf8")) as any

    expect(manifest.collection_mode).toBe("passive_sidecar")
    expect(manifest.behavior_impact).toBe("none")
    expect(trace.manifest.collection_mode).toBe("passive_sidecar")
    expect(trace.manifest.behavior_impact).toBe("none")
  })

  test("keeps agent-visible result bytes and hash unchanged when passive trace writes fail", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-passive-write-failure-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "passive-write-failure.ts")
    const traceRoot = path.join(dir, "trace-root")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { mkdirSync } from "node:fs"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const result = { answer: "stable agent result", files: ["src/pricing.mjs"], exit_code: 0 }`,
        `CaseTrace.configure({ input: { task: "passive write failure" } })`,
        `const trace = CaseTrace.get()`,
        `if (trace) mkdirSync(trace.traceFile)`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello" } })`,
        `CaseTrace.finish({ status: "success", result })`,
        `process.stdout.write(JSON.stringify(result))`,
      ].join("\n"),
    )

    const run = async (enabled: boolean) => {
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: enabled ? "1" : "0",
          OPENCODE_CASE_ID: "passive-write-failure-case",
          OPENCODE_CASE_TRACE_DIR: traceRoot,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stdout = Buffer.from(await new Response(proc.stdout).arrayBuffer())
      const stderr = await new Response(proc.stderr).text()
      return { code, stdout, stderr, hash: createHash("sha256").update(stdout).digest("hex") }
    }

    const baseline = await run(false)
    const failedTrace = await run(true)

    expect(baseline.code).toBe(0)
    expect(failedTrace.code).toBe(0)
    expect(baseline.stderr).toBe("")
    expect(failedTrace.stderr).toContain("[opencode-observability] terminal trace persistence failed")
    expect(failedTrace.stderr).toContain('"trace":false')
    expect(failedTrace.stderr).toContain('"canonical_removed":false')
    expect(failedTrace.stderr).not.toContain("stable agent result")
    expect(failedTrace.stdout).toEqual(baseline.stdout)
    expect(failedTrace.hash).toBe(baseline.hash)
    const caseDir = path.join(traceRoot, "passive-write-failure-case")
    expect((await fs.stat(path.join(caseDir, "trace.json"))).isDirectory()).toBe(true)
    for (const file of [
      "manifest.json",
      "legacy-trace.json",
      "provenance-trace.json",
      "records.jsonl",
      "partial/latest.json",
      "trace.html",
    ]) {
      expect(await exists(path.join(caseDir, file))).toBe(true)
    }

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(caseDir, "provenance-trace.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)

    expect(partial.manifest).toEqual(manifest)
    expect(provenance).toMatchObject({
      trace_version: partial.trace_version,
      manifest,
      records: partial.records,
      dataflow_edges: partial.dataflow_edges,
      artifacts: partial.artifacts,
    })
    expect(legacy).toMatchObject({
      case_id: manifest.case_id,
      run_id: manifest.run_id,
      status: manifest.status,
      result: manifest.result,
    })
    assertJournalReplaysCanonicalTrace(journal, partial)
    assertFinalForcedCheckpointMatchesCanonicalTrace(journal, partial, partial)
  })

  test("persists poisoned journal semantics when only the finalization append fails", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-finalization-append-failure-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "finalization-append-failure.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const result = { answer: "agent result survives finalization append failure", exit_code: 0 }`,
        `CaseTrace.node({ node_id: "before_finalization_failure", kind: "execution.observation", component: "runtime", data: { status: "ready" } })`,
        `const trace = CaseTrace.get() as any`,
        `const durableAppend = trace.writeCausalIRRecord.bind(trace)`,
        `trace.writeCausalIRRecord = (entry: any) => entry.operation === "case.finalized" ? false : durableAppend(entry)`,
        `CaseTrace.finish({ status: "success", result })`,
        `process.stdout.write(JSON.stringify(result))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "finalization-append-failure-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")
    expect(JSON.parse(await new Response(proc.stdout).text())).toEqual({
      answer: "agent result survives finalization append failure",
      exit_code: 0,
    })

    const caseDir = path.join(dir, "finalization-append-failure-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial", "latest.json"), "utf8")) as any
    const journal = await readCausalIRJournal(caseDir)
    const replayTrace = (CausalIRModule as any).replayCausalIRTrace(journal)

    assertCausalIRJournalAudit(journal)
    expect(journal.some((entry: any) => entry.operation === "case.finalized")).toBe(false)
    expect(trace.journal).toMatchObject({
      entry_count: journal.length,
      last_sequence: journal.at(-1)?.sequence,
      last_payload_hash: journal.at(-1)?.payload_hash,
      poisoned: true,
    })
    expect(partial.journal).toEqual(trace.journal)
    expect(replayTrace).toBeDefined()
    expect(replayTrace).not.toEqual(trace)
    expect(replayTrace.journal.poisoned).toBe(false)
    expect(replayCausalIRJournal(journal)).toMatchObject({
      nodes: trace.nodes,
      edges: trace.edges,
      artifacts: trace.artifacts,
      diagnostics: trace.diagnostics,
    })
  })

  test("persists and renders design records", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-design-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "design-record-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const designText = [
      "方案设计：在 checkout 层新增折扣策略接口。",
      "架构边界：pricing 负责折扣，tax 负责税费。",
      "取舍：保持 API 稳定，但增加策略注入。",
      "风险：历史订单回放需要兼容旧字段。",
      "测试策略：补充 pricing 单测和 checkout 集成测试。",
    ].join("\n")
    const diff = [
      "Index: src/pricing.mjs",
      "@@",
      "-  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)",
      "+  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.15)",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.contextSnapshot({ phase: "llm_request", messages: [{ role: "user", content: "设计折扣能力扩展方案" }] })`,
        `CaseTrace.change({ tool_call_id: "call_pricing", files: ["src/pricing.mjs"], intent: "Fix pricing discount cap", diff: ${JSON.stringify(diff)} })`,
        `CaseTrace.designRecord({ source: "final_response", requirement_summary: "设计折扣能力扩展方案", existing_boundaries: "pricing/tax/checkout", selected_solution: ${JSON.stringify(designText)}, tradeoffs: "保持 API 稳定", risks: "历史订单兼容", test_strategy: "pricing 单测和 checkout 集成测试" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "design-record-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "design-record-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    expect(trace.design_records).toHaveLength(1)
    expect(trace.design_records[0].selected_solution.artifact_id).toBeTruthy()

    const provenanceTrace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const change = provenanceTrace.records.find((record: any) => record.event_type === "change")
    const designRecords = provenanceTrace.records.filter((record: any) => record.event_type === "design.record")
    expect(change.data.change_semantics.risk_flags).toBeArray()
    expect(change.data.source_ref_relations).toBeArray()
    expect(designRecords).toHaveLength(1)
    expect(designRecords[0].record_id).toBe(trace.design_records[0].design_id)
    expect(designRecords[0].data.selected_solution.artifact_id).toBeTruthy()
    expect(designRecords[0].data.test_strategy.preview).toContain("pricing")

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Artifacts")
  })

  test("preserves nested semantic schema fields while externalizing raw causal payloads", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-semantic-boundaries-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-boundaries-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const rawSummaryLikePayload = { type: "object", value: "raw-payload-" + "x".repeat(200) }
    const rawPayload = JSON.stringify(rawSummaryLikePayload)
    const largeText = "raw-text-" + "y".repeat(200)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.contextSnapshot({ phase: "llm_request", context_ledger: { algorithm: "head-tail-summary", retained_message_ids: ["message:retained"], dropped_fact_refs: ["evidence:dropped"] } })`,
        `CaseTrace.change({ span_id: "span_change", tool_call_id: "call_change", files: ["src/pricing.mjs"], diff: "- old\\n+ new\\n" + "e".repeat(200) })`,
        `CaseTrace.llmTurn({ turn_id: "turn_semantic", status: "success" })`,
        `CaseTrace.node({ node_id: "llm_semantic", kind: "llm.call", component: "llm", title: "semantic message transforms", data: { message_transforms: [{ node_ref: "node:transform", transform: "MessageV2.toModelMessagesEffect", nested_semantics: { preservation_flags: ["preserved"] } }] } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "repo_fact", summary: "pricing owner", data: { subject: "pricing", predicate: "owner", value: "billing-platform", path: "src/pricing.mjs", line_start: 7, line_end: 7 } })`,
        `CaseTrace.verification({ command: "bun test", exit_code: 0, stdout: ${JSON.stringify(largeText)}, stderr: ${JSON.stringify(largeText)} })`,
        `CaseTrace.node({ node_id: "raw_boundary", kind: "llm.call", component: "llm", title: "raw causal payloads", data: { input: ${rawPayload}, output: ${rawPayload}, messages: [${rawPayload}], tools: { tool: ${rawPayload} }, diff: ${JSON.stringify(largeText)}, stdout: ${JSON.stringify(largeText)}, stderr: ${JSON.stringify(largeText)} } })`,
        `CaseTrace.compaction({ trigger: "auto", previous_summary: ${rawPayload}, serialized_tail: ${rawPayload}, output_summary: ${rawPayload} })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-boundaries-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "semantic-boundaries-case", "trace.json"), "utf8")) as any
    const change = trace.records.find((record: any) => record.event_type === "change")
    const context = trace.records.find((record: any) => record.event_type === "context.pack")
    const transforms = trace.records.find((record: any) => record.record_id === "llm_semantic")
    const fact = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")
    const verification = trace.records.find((record: any) => record.event_type === "verification")
    const rawBoundary = trace.records.find((record: any) => record.record_id === "raw_boundary")
    const compaction = trace.records.find((record: any) => record.event_type === "context.compaction")

    expect(change.data.source_ref_relations[0].source_ref).toContain("tool_call:")
    expect(change.data.source_ref_relations[0].relation).toBe("materialized_by_action")
    expect(change.data.change_semantics.risk_flags).toBeArray()
    expect(change.data.change_semantics.operation_kinds).toBeArray()
    expect(context.data.context_ledger.retained_message_ids[0]).toContain("message:retained")
    expect(context.data.context_ledger.dropped_fact_refs[0]).toContain("evidence:dropped")
    expect(transforms.data.message_transforms[0].node_ref).toContain("node:transform")
    expect(transforms.data.message_transforms[0].nested_semantics.preservation_flags[0]).toContain("preserved")
    expect(fact.data.structured_claim.subject).toBe("pricing")
    expect(fact.data.structured_claim.source_span.path).toBe("src/pricing.mjs")
    expect(verification.data.final_test_result.status).toBe("passed")

    for (const field of ["input", "output", "messages", "tools", "diff", "stdout", "stderr"]) {
      expect(rawBoundary.data[field].artifact_id).toBeTruthy()
      expect(rawBoundary.data[field].payload_ref).toBe(rawBoundary.data[field].artifact_id)
    }
    expect(compaction.data.previous_summary.artifact_id).toBeTruthy()
    expect(compaction.data.serialized_tail.artifact_id).toBeTruthy()
    expect(compaction.data.output_summary.artifact_id).toBeTruthy()
  })

  test("drops design records attached to response segments later demoted from final", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-design-final-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "design-record-final-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const earlyDesignText = [
      "方案设计：这是一次中间代码仓扫描总结。",
      "架构边界：pricing 负责报价，docs 记录设计约束。",
      "设计约束：不允许 hardcode 测试输入，不绕过 public API。",
      "测试策略：稍后继续运行 pricing 单测。",
    ].join("\\n")
    const finalDesignText = [
      "方案设计：最终采用修正折扣上限的通用计算方案。",
      "架构边界：仅修改 pricing 内部常量，保持 public API 不变。",
      "设计约束：不硬编码测试输入，不绕过 public API，保持通用计算逻辑。",
      "测试策略：pricing 单测和 design-quality 测试均通过。",
    ].join("\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const early = CaseTrace.responseOutput({ text: ${JSON.stringify(earlyDesignText)}, response_role: "final_answer", is_final_for_case: true })`,
        `CaseTrace.designRecord({ source: "final_response", selected_solution: ${JSON.stringify(earlyDesignText)}, design_constraints: "不允许 hardcode 测试输入", test_strategy: "稍后运行单测", metadata: { source_segment_id: early?.segment_id } })`,
        `const final = CaseTrace.responseOutput({ text: ${JSON.stringify(finalDesignText)}, response_role: "final_answer", is_final_for_case: true })`,
        `const kept = CaseTrace.designRecord({ source: "final_response", selected_solution: ${JSON.stringify(finalDesignText)}, design_constraints: "保持通用计算逻辑", test_strategy: "pricing 单测和 design-quality 测试均通过", metadata: { source_segment_id: final?.segment_id } })`,
        `CaseTrace.finish({ status: "success" })`,
        `console.log(kept?.design_id)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "design-record-final-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "96",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()
    const keptDesignID = (await new Response(proc.stdout).text()).trim()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "design-record-final-case")
    const legacyTrace = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    const provenanceTrace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const designRecords = provenanceTrace.records.filter((record: any) => record.event_type === "design.record")

    expect(legacyTrace.design_records.map((record: any) => record.design_id)).toEqual([keptDesignID])
    expect(designRecords.map((record: any) => record.record_id)).toEqual([keptDesignID])
    expect(designRecords[0].data.selected_solution.preview).toContain("最终采用")
  })

  test("preserves formal semantic suffixes recursively below the collection boundary", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-formal-suffixes-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "formal-suffixes-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.node({ node_id: "formal_suffixes", kind: "verification", component: "tool", title: "formal suffixes", data: { coverage_semantics: { source_refs: ["ev:price"], quality_flags: ["covered"], nested_semantics: { changed_test_refs: ["chg:test"], changed_production_refs: ["chg:src"], verification_scope_risk_flags: ["tested"] } }, source_refs: ["ev:root"], quality_flags: ["reviewed"] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "formal-suffixes-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "8",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "formal-suffixes-case", "trace.json"), "utf8")) as any
    const record = trace.records.find((item: any) => item.record_id === "formal_suffixes")

    expect(record.data.coverage_semantics.source_refs).toEqual(["ev:price"])
    expect(record.data.coverage_semantics.quality_flags).toEqual(["covered"])
    expect(record.data.coverage_semantics.nested_semantics.changed_test_refs).toEqual(["chg:test"])
    expect(record.data.coverage_semantics.nested_semantics.changed_production_refs).toEqual(["chg:src"])
    expect(record.data.coverage_semantics.nested_semantics.verification_scope_risk_flags).toEqual(["tested"])
    expect(record.data.source_refs).toEqual(["ev:root"])
    expect(record.data.quality_flags).toEqual(["reviewed"])
  })

  test("externalizes long string leaves inside structured semantic subtrees", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-semantic-string-leaves-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-string-leaves-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const longChangeSummary = "change-summary-" + "c".repeat(200)
    const longClaimValue = "claim-value-" + "v".repeat(200)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "Semantic summary boundary", diff: "- old\\n+ new", change_semantics: { changed_line_count: 2, added_line_count: 1, removed_line_count: 1, summary: ${JSON.stringify(longChangeSummary)} } })`,
        `CaseTrace.evidenceFact({ source: "tool", category: "repo_fact", summary: "long structured claim", data: { subject: "pricing", predicate: "owner", value: ${JSON.stringify(longClaimValue)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-string-leaves-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "semantic-string-leaves-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const change = trace.records.find((record: any) => record.event_type === "change")
    const fact = trace.records.find((record: any) => record.event_type === "evidence.semantic_fact")

    for (const summary of [change.data.change_semantics.summary, fact.data.structured_claim.value]) {
      expect(summary.type).toBe("text")
      expect(summary.artifact_id).toBeTruthy()
      expect(summary.payload_ref).toBe(summary.artifact_id)
    }
  })

  test("externalizes high-cardinality formal collections without truncating their contents", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-high-cardinality-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "high-cardinality-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const inputChecks = Array.from({ length: 65 }, (_, index) => `v${index}`)
    const inputChecksByName = Object.fromEntries(inputChecks.map((value, index) => [`check_${index}`, value]))
    const inlineChecks = Array.from({ length: 64 }, (_, index) => `i${index}`)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.node({ node_id: "high_cardinality", kind: "verification", component: "tool", title: "high cardinality", data: { final_test_result: { executed_checks: ${JSON.stringify(inputChecks)}, checks_by_name: ${JSON.stringify(inputChecksByName)}, inline_checks: ${JSON.stringify(inlineChecks)} } } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "high-cardinality-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "8",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const caseDir = path.join(dir, "high-cardinality-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const record = trace.records.find((item: any) => item.record_id === "high_cardinality")
    const checks = record.data.final_test_result.executed_checks
    const checksByName = record.data.final_test_result.checks_by_name
    const arrayArtifact = trace.artifacts.find((item: any) => item.artifact_id === checks.artifact_id)
    const objectArtifact = trace.artifacts.find((item: any) => item.artifact_id === checksByName.artifact_id)

    expect(checks.type).toBe("array")
    expect(checks.artifact_id).toBeTruthy()
    expect(checks.payload_ref).toBe(checks.artifact_id)
    expect(JSON.parse(await fs.readFile(path.join(caseDir, arrayArtifact.path), "utf8"))).toEqual(
      Array.from({ length: 65 }, (_, index) => `v${index}`),
    )
    expect(checksByName.type).toBe("object")
    expect(checksByName.artifact_id).toBeTruthy()
    expect(checksByName.payload_ref).toBe(checksByName.artifact_id)
    expect(JSON.parse(await fs.readFile(path.join(caseDir, objectArtifact.path), "utf8"))).toEqual(
      Object.fromEntries(Array.from({ length: 65 }, (_, index) => [`check_${index}`, `v${index}`])),
    )
    expect(record.data.final_test_result.inline_checks).toEqual(Array.from({ length: 64 }, (_, index) => `i${index}`))
  })

  test("keeps generated array summaries intact while rejecting summary-shaped business data", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-summary-idempotence-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "summary-idempotence-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace, summarizeJson } from ${JSON.stringify(traceModule)}`,
        `const generated = summarizeJson(["one", "two", "three"])`,
        `CaseTrace.node({ node_id: "summary_idempotence", kind: "verification", component: "tool", title: "summary idempotence", data: { generated, business_payload: { type: "null", value: null, artifact_id: "not-a-summary" } } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "summary-idempotence-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "16",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    expect(await new Response(proc.stderr).text()).toBe("")

    const trace = JSON.parse(await fs.readFile(path.join(dir, "summary-idempotence-case", "trace.json"), "utf8")) as any
    const record = trace.records.find((item: any) => item.record_id === "summary_idempotence")

    expect(record.data.generated.type).toBe("array")
    expect(record.data.generated.preview).toBe('["one","two","th')
    expect(record.data.business_payload.type).toBe("object")
    expect(record.data.business_payload.artifact_id).toBeTruthy()
  })

  test("closes the exact Astropy producer claim and artifact bundle facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task5-astropy-closure-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "astropy-closure.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const response =
      "All 11 tests pass. The fix was a one-character change in `astropy/modeling/separable.py:245`: `= 1` → `= right`. When `_cstack` handled a non-Model `right` operand (i.e., a pre-computed separability matrix from a nested compound model), it was filling the block with all 1s instead of the actual matrix values, causing nested compound models to appear non-separable."
    const artifactPayload = `task-5-artifact-closure:${"verifiable semantic evidence ".repeat(100)}`
    const expectedArtifactText = JSON.stringify({ payload: artifactPayload })
    const expectedArtifactBytes = Buffer.from(expectedArtifactText, "utf8")
    const expectedArtifactSha256 = createHash("sha256").update(expectedArtifactBytes).digest("hex")
    const expectedSemanticPrefix = expectedArtifactText.slice(0, 512)
    const expectedSemanticBytes = Buffer.from(expectedSemanticPrefix, "utf8")
    const expectedSemanticSha256 = createHash("sha256").update(expectedSemanticBytes).digest("hex")

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `CaseTrace.configure({ subjectRevision: "git:task5-current-producer" })`,
          `CaseTrace.responseOutput({ response_role: "final_answer", text: ${JSON.stringify(response)} })`,
          `CaseTrace.event({ component: "context", event_type: "context.before_compaction", data: { payload: ${JSON.stringify(artifactPayload)} } })`,
          `CaseTrace.finish({ status: "success" })`,
        ].join("\n"),
      )

      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task5-astropy-closure",
          OPENCODE_CASE_TRACE_DIR: dir,
          OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "512",
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      expect(await proc.exited).toBe(0)
      expect(await new Response(proc.stderr).text()).toBe("")

      const caseDir = path.join(dir, "task5-astropy-closure")
      const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
      const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
      const claimTexts = claims.map((record: any) => record.data.text)

      expect(trace.manifest.subject_revision).toBe("git:task5-current-producer")
      expect(trace.manifest.subject_revision_provenance).toMatchObject({
        method: "case_trace_config",
        source: "CaseTraceConfig.subjectRevision",
        bound_at: "case_start",
        case_id: trace.manifest.case_id,
        run_id: trace.manifest.run_id,
      })
      expect(claimTexts).toEqual([
        "All 11 tests pass.",
        "The fix was a one-character change in `astropy/modeling/separable.py:245`: `= 1` → `= right`.",
        "When `_cstack` handled a non-Model `right` operand (i.e., a pre-computed separability matrix from a nested compound model), it was filling the block with all 1s instead of the actual matrix values, causing nested compound models to appear non-separable.",
      ])
      expect(claimTexts.some((text: string) => /^[,，;；)）\]］}｝]/.test(text))).toBe(false)
      for (const claim of claims) {
        expectResponseClaimAtomizationClosure(claim.data)
        const [start, end] = claim.data.source_byte_range
        expect(Buffer.from(response).subarray(start, end).toString()).toContain(claim.data.text)
      }

      const artifact = trace.artifacts.find((item: any) => item.label === "context.context.before_compaction.data")
      expect(artifact.availability).toBe("bundled")
      expect(typeof artifact.path).toBe("string")
      expect(path.isAbsolute(artifact.path)).toBe(false)
      expect(artifact.path.split(path.sep)).not.toContain("..")
      const artifactBytes = await fs.readFile(path.join(caseDir, artifact.path))
      expect(artifactBytes.equals(expectedArtifactBytes)).toBe(true)
      expect(artifactBytes.toString("utf8")).toBe(expectedArtifactText)
      expect(artifact.byte_length).toBe(expectedArtifactBytes.byteLength)
      expect(artifact.content_hash).toBe(expectedArtifactSha256.slice(0, 16))
      expect(artifact.semantic_slices).toEqual([
        {
          byte_range: [0, expectedSemanticBytes.byteLength],
          content: expectedSemanticPrefix,
          hash: expectedSemanticSha256.slice(0, 16),
          truncated: true,
        },
      ])
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })
})
