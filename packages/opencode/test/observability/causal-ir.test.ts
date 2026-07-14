import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import {
  CausalIRStore,
  projectProvenanceTrace,
  replayCausalIRJournal,
  type CausalIRJournalEntry,
} from "@/observability/causal-ir"

function node(nodeID: string, data: Record<string, unknown> = {}) {
  return {
    node_id: nodeID,
    kind: "decision",
    component: "task",
    timestamp: "2026-07-14T00:00:00.000Z",
    time_ms: 1,
    data,
  }
}

function edge(edgeID: string) {
  return {
    edge_id: edgeID,
    from: { type: "node", id: "node_1" },
    to: { type: "node", id: "node_2" },
    relation: "produced",
  }
}

describe("causal IR store", () => {
  test("replays node creation and update into the same snapshot", () => {
    const journal: unknown[] = []
    const store = new CausalIRStore({ runID: "run_1", caseID: "case_1", append: (entry) => journal.push(entry) })
    const created = store.createNode(node("node_1", { chosen_action: "read" }))
    created.data = { chosen_action: "edit" }
    store.updateNode(created)

    expect(replayCausalIRJournal(journal).nodes).toEqual(store.snapshot().nodes)
    expect(journal.map((entry: any) => entry.operation)).toEqual(["node.created", "node.updated"])
    expect(journal.map((entry: any) => entry.record_type)).toEqual(["node", "node.update"])
  })

  test("replays a node replacement journal prefix without a later lifecycle snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_nodes", caseID: "case_nodes", append: (entry) => journal.push(entry) })
    store.createNode(node("node_removed"))
    store.createNode(node("node_retained", { chosen_action: "keep" }))
    store.replaceNodes([node("node_replacement", { chosen_action: "replace" })])

    expect(journal.map((entry) => entry.operation)).toEqual(["node.created", "node.created", "case.checkpointed"])
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
    expect(replayCausalIRJournal(journal).nodes.map((item) => item.node_id)).toEqual(["node_replacement"])
  })

  test("replays an edge replacement journal prefix without a later lifecycle snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_edges", caseID: "case_edges", append: (entry) => journal.push(entry) })
    store.createEdge(edge("edge_removed"))
    store.createEdge(edge("edge_retained"))
    store.replaceEdges([edge("edge_replacement")])

    expect(journal.map((entry) => entry.operation)).toEqual(["edge.created", "edge.created", "case.checkpointed"])
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
    expect(replayCausalIRJournal(journal).edges.map((item) => item.edge_id)).toEqual(["edge_replacement"])
  })

  test("replays an artifact reuse journal prefix without a lifecycle snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_artifact",
      caseID: "case_artifact",
      append: (entry) => journal.push(entry),
    })
    const artifact = store.createArtifact({
      artifact_id: "artifact_1",
      hash: "hash_1",
      path: "artifacts/one.json",
      occurrences: 1,
      metadata: { source: "initial", reason: "first use" },
    })
    artifact.occurrences = 2
    artifact.metadata = { source: "reused", reason: "same payload" }
    store.reuseArtifact(artifact)

    expect(journal.map((entry) => entry.operation)).toEqual(["artifact.created", "artifact.reused"])
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
    expect(replayCausalIRJournal(journal).artifacts).toEqual([artifact])
    expect(journal[1]?.record_type).toBe("artifact.reuse")
  })

  test("replays a checkpoint prefix with the snapshot current at the checkpoint", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_checkpoint",
      caseID: "case_checkpoint",
      append: (entry) => journal.push(entry),
    })
    const created = store.createNode(node("node_1", { chosen_action: "read" }))
    created.data = { chosen_action: "checkpointed" }
    store.checkpoint({ phase: "partial" })

    const checkpoints = journal.filter((entry) => entry.operation === "case.checkpointed")
    expect(checkpoints).toHaveLength(1)
    expect(replayCausalIRJournal(journal.slice(0, checkpoints[0]!.sequence))).toEqual(store.snapshot())
  })

  test("replays a finalization prefix with the snapshot current at finalization", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_final", caseID: "case_final", append: (entry) => journal.push(entry) })
    const created = store.createNode(node("node_1", { chosen_action: "read" }))
    created.data = { chosen_action: "finalized" }
    store.finalize({ status: "success" })

    const finalizations = journal.filter((entry) => entry.operation === "case.finalized")
    expect(finalizations).toHaveLength(1)
    expect(replayCausalIRJournal(journal.slice(0, finalizations[0]!.sequence))).toEqual(store.snapshot())
  })

  test("rebuilds node payload hashes from a replacement snapshot and clears removed node hashes", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_node_hash", caseID: "case_node_hash", append: (entry) => journal.push(entry) })
    store.createNode(node("node_1", { chosen_action: "original" }))
    const replacement = node("node_1", { chosen_action: "replacement" })
    store.replaceNodes([replacement])
    replacement.data = { chosen_action: "updated" }
    store.updateNode(replacement)

    const expectedReplacementHash = createHash("sha256")
      .update('{"component":"task","data":{"chosen_action":"replacement"},"kind":"decision","node_id":"node_1","time_ms":1,"timestamp":"2026-07-14T00:00:00.000Z"}')
      .digest("hex")
    expect(journal[2]?.previous_payload_hash).toBe(expectedReplacementHash)

    const deletedJournal: CausalIRJournalEntry[] = []
    const deletedStore = new CausalIRStore({
      runID: "run_node_deleted",
      caseID: "case_node_deleted",
      append: (entry) => deletedJournal.push(entry),
    })
    deletedStore.createNode(node("node_deleted"))
    deletedStore.replaceNodes([])
    deletedStore.createNode(node("node_deleted", { chosen_action: "recreated" }))

    expect(deletedJournal[2]?.previous_payload_hash).toBeUndefined()
  })

  test("clears removed edge payload hashes after replacement", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_edge_hash", caseID: "case_edge_hash", append: (entry) => journal.push(entry) })
    store.createEdge(edge("edge_deleted"))
    store.replaceEdges([])
    store.createEdge(edge("edge_deleted"))

    expect(journal[2]?.previous_payload_hash).toBeUndefined()
  })

  test("chains canonical payload hashes using locale-independent lexical key ordering", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_hash", caseID: "case_hash", append: (entry) => journal.push(entry) })
    const created = store.createNode(node("node_1", { z: 0, "ä": 2, a: 1 }))
    created.data = { "ä": 2, a: 1, z: 3 }
    store.updateNode(created)

    const expectedCreatedHash = createHash("sha256")
      .update('{"component":"task","data":{"a":1,"z":0,"ä":2},"kind":"decision","node_id":"node_1","time_ms":1,"timestamp":"2026-07-14T00:00:00.000Z"}')
      .digest("hex")
    expect(journal[0]?.payload_hash).toBe(expectedCreatedHash)
    expect(journal[1]?.previous_payload_hash).toBe(journal[0]?.payload_hash)
  })

  test("serializes integer-like keys in explicit lexical order for canonical payload hashes", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_integer_keys",
      caseID: "case_integer_keys",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("node_1", { "2": "two", "10": "ten", a: "letter" }))

    const expectedHash = createHash("sha256")
      .update(
        '{"component":"task","data":{"10":"ten","2":"two","a":"letter"},"kind":"decision","node_id":"node_1","time_ms":1,"timestamp":"2026-07-14T00:00:00.000Z"}',
      )
      .digest("hex")
    expect(journal[0]?.payload_hash).toBe(expectedHash)
  })

  test("hashes sparse array slots as JSON null values", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_sparse", caseID: "case_sparse", append: (entry) => journal.push(entry) })
    store.createNode(node("empty", { values: [] }))
    store.createNode(node("hole", { values: new Array(1) }))
    store.createNode(node("mixed", { values: [1, , 2] }))

    const expectedHoleHash = createHash("sha256")
      .update('{"component":"task","data":{"values":[null]},"kind":"decision","node_id":"hole","time_ms":1,"timestamp":"2026-07-14T00:00:00.000Z"}')
      .digest("hex")
    const expectedMixedHash = createHash("sha256")
      .update('{"component":"task","data":{"values":[1,null,2]},"kind":"decision","node_id":"mixed","time_ms":1,"timestamp":"2026-07-14T00:00:00.000Z"}')
      .digest("hex")

    expect(journal[0]?.payload_hash).not.toBe(journal[1]?.payload_hash)
    expect(journal[1]?.payload_hash).toBe(expectedHoleHash)
    expect(journal[2]?.payload_hash).toBe(expectedMixedHash)
  })

  test("replays diagnostic creation into the snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_diagnostic",
      caseID: "case_diagnostic",
      append: (entry) => journal.push(entry),
    })
    const diagnostic = store.createDiagnostic({
      diagnostic_id: "diagnostic_1",
      level: "warning",
      message: "missing source reference",
    })

    expect(journal.map((entry) => entry.operation)).toEqual(["diagnostic.created"])
    expect(replayCausalIRJournal(journal).diagnostics).toEqual([diagnostic])
  })

  test("assigns contiguous journal sequence values", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_sequence", caseID: "case_sequence", append: (entry) => journal.push(entry) })
    store.createNode(node("node_1"))
    store.createDiagnostic({ diagnostic_id: "diagnostic_1", message: "warning" })
    store.checkpoint({ phase: "partial" })
    store.finalize({ status: "success" })

    expect(journal.map((entry) => entry.sequence)).toEqual([1, 2, 3, 4])
  })

  test("preserves caller references while isolating emitted journal payloads", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_refs", caseID: "case_refs", append: (entry) => journal.push(entry) })
    const created = node("node_1", { chosen_action: "read" })
    store.createNode(created)
    created.data!.chosen_action = "edit"

    expect(store.nodes[0]).toBe(created)
    expect(journal[0]?.data).toEqual(node("node_1", { chosen_action: "read" }))
  })

  test("preserves an unknown original relation without making it attribution eligible", () => {
    const store = new CausalIRStore({ runID: "run_1", caseID: "case_1" })
    store.createEdge({
      edge_id: "edge_1",
      from: { type: "node", id: "a" },
      to: { type: "node", id: "b" },
      relation: "custom_future_relation",
    })

    const ir = store.snapshot()
    expect(ir.edges[0]?.original_relation).toBe("custom_future_relation")
    expect(ir.edges[0]?.normalized_relation).toBe("derived_from")
    expect(ir.edges[0]?.eligible_for_attribution).toBe(false)
    expect(ir.diagnostics[0]?.kind).toBe("unknown_relation")
  })

  test("projects canonical nodes into the existing provenance record contract", () => {
    const store = new CausalIRStore({ runID: "run_1", caseID: "case_1" })
    store.createNode(node("node_1"))
    store.createEdge({
      edge_id: "edge_1",
      from: { type: "node", id: "node_1" },
      to: { type: "node", id: "node_2" },
      relation: "source_to_observation",
      evidence_tier: "direct",
      eligible_for_attribution: true,
      derivation_method: "explicit_relation",
      metadata: { retained: true },
    })

    const projection = projectProvenanceTrace(store.snapshot(), {
      traceVersion: "6.0",
      manifest: { case_id: "case_1", run_id: "run_1" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })

    expect(projection.records[0]?.record_id).toBe("node_1")
    expect(projection.dataflow_edges[0]?.relation).toBe("derived_from")
    expect(projection.dataflow_edges[0]?.metadata).toEqual({
      retained: true,
      original_relation: "source_to_observation",
      evidence_tier: "direct",
      eligible_for_attribution: true,
      derivation_method: "explicit_relation",
    })
  })
})
