import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { CausalIRStore, replayCausalIRJournal, type CausalIRJournalEntry } from "@/observability/causal-ir"

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

  test("replays the complete final snapshot after replacements, reuse, diagnostics, and lifecycle facts", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_complete",
      caseID: "case_complete",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("node_removed"))
    store.createNode(node("node_retained", { chosen_action: "keep" }))
    store.replaceNodes([node("node_replacement", { chosen_action: "replace" })])
    store.createEdge(edge("edge_removed"))
    store.createEdge(edge("edge_retained"))
    store.replaceEdges([edge("edge_replacement")])

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
    store.createDiagnostic({ diagnostic_id: "diag_1", code: "reused_artifact", detail: { artifact_id: artifact.artifact_id } })
    store.checkpoint({ phase: "partial" })
    store.finalize({ status: "success" })

    const replayed = replayCausalIRJournal(journal)
    expect(replayed).toEqual(store.snapshot())
    expect(replayed.runID).toBe("run_complete")
    expect(replayed.caseID).toBe("case_complete")
    expect(replayed.nodes.map((item) => item.node_id)).toEqual(["node_replacement"])
    expect(replayed.edges.map((item) => item.edge_id)).toEqual(["edge_replacement"])
    expect(replayed.artifacts).toEqual([artifact])
    expect(replayed.diagnostics).toEqual([{ diagnostic_id: "diag_1", code: "reused_artifact", detail: { artifact_id: "artifact_1" } }])
    expect(journal.map((entry) => entry.sequence)).toEqual(Array.from({ length: journal.length }, (_, index) => index + 1))
    expect(journal.every((entry) => entry.run_id === "run_complete" && entry.case_id === "case_complete")).toBe(true)
    expect(journal.find((entry) => entry.operation === "artifact.reused")?.record_type).toBe("artifact.reuse")
    expect(journal.filter((entry) => entry.operation === "case.checkpointed").every((entry) => entry.data)).toBe(true)
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

  test("preserves caller references while isolating emitted journal payloads", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_refs", caseID: "case_refs", append: (entry) => journal.push(entry) })
    const created = node("node_1", { chosen_action: "read" })
    store.createNode(created)
    created.data!.chosen_action = "edit"

    expect(store.nodes[0]).toBe(created)
    expect(journal[0]?.data).toEqual(node("node_1", { chosen_action: "read" }))
  })
})
