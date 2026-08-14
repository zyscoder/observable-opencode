import { expect, spyOn, test } from "bun:test"
import { Database } from "bun:sqlite"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  canonicalCausalIREdge,
  canonicalCausalIRNode,
  causalIRPayloadHash,
  type CausalIRJournalEntry,
} from "@/observability/causal-ir"
import { materializeTrace } from "@/observability/trace-materializer"

const OWNER_COUNT = 2_048

test("streams high-cardinality diagnostic owners without graph-sized SQLite all() retention", () => {
  const caseDir = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-trace-diagnostics-"))
  const recordsFile = path.join(caseDir, "records.jsonl")
  const fd = fs.openSync(recordsFile, "w")
  const runID = "run_materializer_diagnostics"
  const caseID = "case-materializer-diagnostics"
  let sequence = 0
  const append = (
    operation: CausalIRJournalEntry["operation"],
    recordType: string,
    entityID: string,
    data: unknown,
  ) => {
    const entry: CausalIRJournalEntry = {
      sequence: ++sequence,
      time: "2026-08-14T12:00:00.000Z",
      run_id: runID,
      case_id: caseID,
      operation,
      record_type: recordType,
      entity_id: entityID,
      data,
      payload_hash: causalIRPayloadHash(data),
    }
    fs.writeSync(fd, `${JSON.stringify(entry)}\n`)
  }

  const runStart = canonicalCausalIRNode(
    {
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-14T12:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { run_id: runID, case_id: caseID },
    },
    { runID, caseID, sequence: 1 },
  )
  append("node.created", "run.start", runStart.node_id, runStart)

  const ownerIDs: string[] = []
  const affectedOwners: Array<{ owner_type: "edge"; owner_id: string; field: "from" }> = []
  for (let index = 0; index < OWNER_COUNT; index++) {
    const suffix = String(index).padStart(5, "0")
    const nodeID = `alias_owner_${suffix}`
    const edgeID = `unresolved_owner_${suffix}`
    ownerIDs.push(nodeID)
    affectedOwners.push({ owner_type: "edge", owner_id: edgeID, field: "from" })
    const node = canonicalCausalIRNode(
      {
        node_id: nodeID,
        kind: "response.output",
        component: "result",
        timestamp: "2026-08-14T12:00:01.000Z",
        time_ms: index + 1,
        status: "success",
        aliases: ["shared_alias"],
        data: { text: suffix },
      },
      { runID, caseID, sequence: index + 2 },
    )
    append("node.created", "response.output", node.node_id, node)
    const edge = {
      ...canonicalCausalIREdge({
        edge_id: edgeID,
        from: { type: "verification", id: "missing_verification" },
        to: { type: "external", id: `sink_${suffix}` },
        relation: "derived_from",
      }),
      from: {
        ref_type: "external" as const,
        ref_id: "missing_verification",
        legacy_ref: "verification:missing_verification",
      },
    }
    append("edge.created", "edge", edge.edge_id, edge)
  }
  append("case.runtime_closed", "runtime_close", caseID, {
    format: "runtime_close",
    status: "success",
    closed_at: "2026-08-14T12:00:02.000Z",
    manifest: { case_id: caseID, run_id: runID },
  })
  fs.fsyncSync(fd)
  fs.closeSync(fd)

  const originalQuery = Database.prototype.query
  const query = spyOn(Database.prototype, "query").mockImplementation(function (this: Database, sql: string) {
    const statement = originalQuery.call(this, sql)
    if (
      sql.includes("SELECT node_id FROM aliases WHERE alias") ||
      sql.includes("SELECT owner_type, owner_id, field FROM unresolved_occurrences")
    ) {
      statement.all = () => {
        throw new Error("graph-sized diagnostic query used all()")
      }
    }
    return statement
  } as typeof Database.prototype.query)

  try {
    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(fs.readFileSync(result.traceFile, "utf8"))

    expect(trace.diagnostics.map((item: { kind: string }) => item.kind)).toEqual(["alias_collision", "unresolved_ref"])
    expect(trace.diagnostics.find((item: { kind: string }) => item.kind === "alias_collision")).toEqual({
      diagnostic_id: `alias_collision:${causalIRPayloadHash("shared_alias").slice(0, 16)}`,
      kind: "alias_collision",
      level: "warning",
      message: "Ambiguous causal alias: shared_alias",
      alias: "shared_alias",
      owner_ids: ownerIDs,
    })
    expect(trace.diagnostics.find((item: { kind: string }) => item.kind === "unresolved_ref")).toEqual({
      diagnostic_id: `unresolved_ref:${causalIRPayloadHash("verification:missing_verification").slice(0, 16)}`,
      kind: "unresolved_ref",
      level: "warning",
      message: "Unresolved causal reference: verification:missing_verification",
      legacy_ref: "verification:missing_verification",
      occurrence_count: OWNER_COUNT,
      affected_owners: affectedOwners,
      owner_type: "edge",
      field: "from",
      edge_id: "unresolved_owner_00000",
    })
  } finally {
    query.mockRestore()
    fs.rmSync(caseDir, { recursive: true, force: true })
  }
})
