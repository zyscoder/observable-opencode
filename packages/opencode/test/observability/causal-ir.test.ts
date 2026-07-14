import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import {
  CausalIRStore,
  projectProvenanceTrace,
  replayCausalIRJournal,
  type CausalIRJournalEntry,
  type CausalIRNodeInput,
} from "@/observability/causal-ir"
import type { ProvenanceRecord, TraceArtifact } from "@/observability/case-trace"
import { RELATION_MIGRATIONS } from "@/observability/trace-semantic-contract"

function node(nodeID: string, data: Record<string, unknown> = {}): CausalIRNodeInput {
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

function relationEdge(edgeID: string, relation: string) {
  return {
    edge_id: edgeID,
    from: { type: "node", id: `${edgeID}_from` },
    to: { type: "node", id: `${edgeID}_to` },
    relation,
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
      evidence_tier: "confirmed",
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
      evidence_tier: "confirmed",
      eligible_for_attribution: true,
      derivation_method: "explicit_relation",
    })
  })

  test("projects the legacy final claim kind as response output", () => {
    const store = new CausalIRStore({ runID: "run_final_claim", caseID: "case_final_claim" })
    store.createNode({
      node_id: "final_claim_1",
      kind: "final.claim",
      component: "result",
      timestamp: "2026-07-14T01:00:00.000Z",
      time_ms: 10,
      data: { segment_id: "segment_1" },
    })

    const projection = projectProvenanceTrace(store.snapshot(), {
      traceVersion: "5.6",
      manifest: { case_id: "case_final_claim", run_id: "run_final_claim" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })

    expect(projection.records).toHaveLength(1)
    expect(projection.records[0]?.event_type).toBe("response.output")
  })

  test("projects every formal provenance record field", () => {
    const store = new CausalIRStore({ runID: "run_record", caseID: "case_record" })
    store.createNode({
      node_id: "record_1",
      kind: "tool.result",
      component: "tool",
      span_id: "span_1",
      parent_span_id: "span_parent",
      timestamp: "2026-07-14T02:00:00.000Z",
      time_ms: 20,
      title: "Tool result",
      status: "success",
      duration_ms: 12,
      token_usage: {
        input: 3,
        output: 5,
        reasoning: 0,
        cached_input: 0,
        cache_write: 0,
        total: 8,
        cost: 0,
      },
      error: { message: "retained diagnostic" },
      input_refs: ["tool_call:call_1"],
      output_refs: ["tool_result:result_1"],
      source_refs: ["source:source_1"],
      source_locations: [{ path: "src/index.ts", line_start: 7, line_end: 9 }],
      typed_resources: [{ type: "file", uri: "file:///src/index.ts" }],
      artifact_refs: ["artifact_1"],
      data: {
        result: "ok",
        duration_ms: 12,
        token_usage: { input: 3, output: 5, total: 8 },
        error: { message: "retained diagnostic" },
      },
      metadata: { source: "fixture" },
    })

    const projection = projectProvenanceTrace(store.snapshot(), {
      traceVersion: "5.6",
      manifest: { case_id: "case_record", run_id: "run_record" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })
    const expected = {
      record_id: "record_1",
      component: "tool",
      event_type: "tool.result",
      span_id: "span_1",
      parent_span_id: "span_parent",
      timestamp: "2026-07-14T02:00:00.000Z",
      time_ms: 20,
      title: "Tool result",
      status: "success",
      duration_ms: 12,
      token_usage: {
        input: 3,
        output: 5,
        reasoning: 0,
        cached_input: 0,
        cache_write: 0,
        total: 8,
        cost: 0,
      },
      error: { message: "retained diagnostic" },
      input_refs: ["tool_call:call_1"],
      output_refs: ["tool_result:result_1"],
      source_refs: ["source:source_1"],
      source_locations: [{ path: "src/index.ts", line_start: 7, line_end: 9 }],
      typed_resources: [{ type: "file", uri: "file:///src/index.ts" }],
      artifact_refs: ["artifact_1"],
      data: {
        result: "ok",
        duration_ms: 12,
        token_usage: { input: 3, output: 5, total: 8 },
        error: { message: "retained diagnostic" },
      },
      metadata: { source: "fixture" },
    } satisfies ProvenanceRecord

    expect(projection.records[0]).toEqual(expected)
  })

  test("projects legacy edge endpoint references and labels", () => {
    const store = new CausalIRStore({ runID: "run_edge_projection", caseID: "case_edge_projection" })
    store.createEdge({
      edge_id: "edge_legacy",
      from: { type: "final_response_evidence", id: "segment_1", label: "final.claim" },
      to: { type: "response_claim", id: "claim_1", label: "final.claim" },
      relation: "evidence_to_response",
      label: "Final response evidence supports final claim",
    })

    const projection = projectProvenanceTrace(store.snapshot(), {
      traceVersion: "5.6",
      manifest: { case_id: "case_edge_projection", run_id: "run_edge_projection" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })

    expect(projection.dataflow_edges[0]).toMatchObject({
      from: { type: "response_segment", id: "segment_1", label: "response.output" },
      to: { type: "response_claim", id: "claim_1", label: "response.output" },
      relation: "supported_response",
      label: "response output supports response output",
    })
  })

  test("preserves the caller manifest, artifacts, and raw span and event metrics", () => {
    const store = new CausalIRStore({ runID: "run_envelope", caseID: "case_envelope" })
    store.createNode(node("node_envelope"))
    const artifact = {
      artifact_id: "artifact_envelope",
      kind: "json",
      label: "Envelope artifact",
      path: "artifacts/envelope.json",
      length: 42,
      hash: "hash_envelope",
      preview: "{\"ok\":true}",
      created_at: "2026-07-14T03:00:00.000Z",
      dedupe_key: "json:hash_envelope",
      occurrences: 2,
    } satisfies TraceArtifact
    store.createArtifact(artifact)
    const manifest = {
      case_id: "case_envelope",
      run_id: "run_envelope",
      collection_mode: "passive_sidecar",
      behavior_impact: "none",
      files: { provenance_trace: "provenance-trace.json" },
    }
    const metrics = {
      spans: 17,
      events: 29,
      records: 999,
      dataflow_edges: 999,
      artifacts: 999,
      token_usage: { input: 11, output: 13, total: 24 },
      stream_summary: { chunks: 31 },
      trace_health: { issues: [], custom_health_counter: 37 },
    }

    const projection = projectProvenanceTrace(store.snapshot(), { traceVersion: "5.6", manifest, metrics })

    expect(projection.manifest).toEqual(manifest)
    expect(projection.artifacts).toEqual([artifact])
    expect(projection.metrics).toEqual({
      ...metrics,
      records: 1,
      dataflow_edges: 0,
      artifacts: 1,
    })
  })

  test("exposes relation migrations as an immutable policy", () => {
    expect(Object.isFrozen(RELATION_MIGRATIONS)).toBe(true)
  })

  test("does not let edge metadata make an unknown relation attributable", () => {
    const store = new CausalIRStore({ runID: "run_metadata", caseID: "case_metadata" })
    store.createEdge({
      edge_id: "edge_metadata",
      from: { type: "node", id: "node_1" },
      to: { type: "node", id: "node_2" },
      relation: "custom_metadata_relation",
      metadata: {
        normalized_relation: "produced",
        evidence_tier: "direct",
        eligible_for_attribution: true,
        derivation_method: "metadata_override",
      },
    })

    expect(store.edges[0]).toMatchObject({
      original_relation: "custom_metadata_relation",
      normalized_relation: "derived_from",
      evidence_tier: "confirmed",
      eligible_for_attribution: false,
      derivation_method: "metadata_override",
    })
    expect(store.diagnostics).toHaveLength(1)
  })

  test("does not diagnose a known relation that is explicitly attribution ineligible", () => {
    const store = new CausalIRStore({ runID: "run_known", caseID: "case_known" })
    store.createEdge({
      edge_id: "edge_known",
      from: { type: "node", id: "node_1" },
      to: { type: "node", id: "node_2" },
      relation: "produced",
      eligible_for_attribution: false,
    })

    expect(store.edges[0]?.eligible_for_attribution).toBe(false)
    expect(store.diagnostics).toEqual([])
  })

  test("always makes temporal advisory evidence attribution ineligible", () => {
    const store = new CausalIRStore({ runID: "run_temporal", caseID: "case_temporal" })
    store.createEdge({
      edge_id: "edge_temporal",
      from: { type: "node", id: "node_1" },
      to: { type: "node", id: "node_2" },
      relation: "produced",
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: true,
      metadata: { eligible_for_attribution: true },
    })

    expect(store.edges[0]).toMatchObject({
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
    })
    expect(store.diagnostics).toEqual([])
  })

  test("falls back to a schema evidence tier when replay input contains an invalid value", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T04:00:00.000Z",
        run_id: "run_invalid_tier",
        case_id: "case_invalid_tier",
        operation: "edge.created",
        record_type: "edge",
        entity_id: "edge_invalid_tier",
        data: {
          edge_id: "edge_invalid_tier",
          from: { type: "node", id: "node_1" },
          to: { type: "node", id: "node_2" },
          relation: "produced",
          evidence_tier: "direct",
        },
      },
    ])

    expect(replayed.edges[0]?.evidence_tier).toBe("confirmed")
  })

  test("updates and removes an unknown diagnostic when the same edge ID changes relation", () => {
    const store = new CausalIRStore({ runID: "run_same_id", caseID: "case_same_id" })
    store.createEdge(relationEdge("edge_same", "custom_alpha"))
    store.createEdge(relationEdge("edge_same", "custom_beta"))

    expect(store.edges).toHaveLength(1)
    expect(store.diagnostics).toEqual([
      expect.objectContaining({
        diagnostic_id: "unknown_relation:edge_same",
        edge_id: "edge_same",
        relation: "custom_beta",
        message: "Unknown causal relation: custom_beta",
      }),
    ])

    store.createEdge(relationEdge("edge_same", "produced"))

    expect(store.edges).toHaveLength(1)
    expect(store.diagnostics).toEqual([])
  })

  test("removes unknown diagnostics when replaceEdges deletes their edges", () => {
    const store = new CausalIRStore({ runID: "run_delete", caseID: "case_delete" })
    store.createEdge(relationEdge("edge_deleted", "custom_deleted"))

    store.replaceEdges([])

    expect(store.edges).toEqual([])
    expect(store.diagnostics).toEqual([])
  })

  test("tracks one current diagnostic for each unknown edge", () => {
    const store = new CausalIRStore({ runID: "run_multiple", caseID: "case_multiple" })
    store.replaceEdges([
      relationEdge("edge_alpha", "custom_alpha"),
      { ...relationEdge("edge_known", "produced"), eligible_for_attribution: false },
      relationEdge("edge_beta", "custom_beta"),
    ])

    expect(store.diagnostics).toEqual([
      expect.objectContaining({ diagnostic_id: "unknown_relation:edge_alpha", relation: "custom_alpha" }),
      expect.objectContaining({ diagnostic_id: "unknown_relation:edge_beta", relation: "custom_beta" }),
    ])
  })

  test("canonicalizes an old edge payload and synthesizes its diagnostic at an edge-created journal prefix", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T05:00:00.000Z",
        run_id: "run_prefix",
        case_id: "case_prefix",
        operation: "edge.created",
        record_type: "edge",
        entity_id: "edge_prefix",
        data: relationEdge("edge_prefix", "custom_prefix"),
      },
    ])

    expect(replayed.edges).toEqual([
      expect.objectContaining({
        edge_id: "edge_prefix",
        original_relation: "custom_prefix",
        normalized_relation: "derived_from",
        evidence_tier: "confirmed",
        eligible_for_attribution: false,
        derivation_method: "unknown_relation_fallback",
      }),
    ])
    expect(replayed.diagnostics).toEqual([
      expect.objectContaining({
        diagnostic_id: "unknown_relation:edge_prefix",
        relation: "custom_prefix",
      }),
    ])
  })

  test("replays a canonical-shaped old payload from its lossless original relation", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T05:30:00.000Z",
        run_id: "run_original_relation",
        case_id: "case_original_relation",
        operation: "edge.created",
        record_type: "edge",
        entity_id: "edge_original_relation",
        data: {
          ...relationEdge("edge_original_relation", "derived_from"),
          original_relation: "custom_preserved_relation",
          normalized_relation: "produced",
          evidence_tier: "confirmed",
          eligible_for_attribution: true,
        },
      },
    ])

    expect(replayed.edges[0]).toMatchObject({
      original_relation: "custom_preserved_relation",
      normalized_relation: "derived_from",
      eligible_for_attribution: false,
    })
    expect(replayed.diagnostics[0]).toMatchObject({
      edge_id: "edge_original_relation",
      relation: "custom_preserved_relation",
    })
  })

  test("reconciles stale unknown diagnostics after replaying same-ID relation changes", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T06:00:00.000Z",
        run_id: "run_replay_replace",
        case_id: "case_replay_replace",
        operation: "edge.created",
        record_type: "edge",
        entity_id: "edge_replay_replace",
        data: relationEdge("edge_replay_replace", "custom_old"),
      },
      {
        sequence: 2,
        time: "2026-07-14T06:00:01.000Z",
        run_id: "run_replay_replace",
        case_id: "case_replay_replace",
        operation: "diagnostic.created",
        record_type: "diagnostic",
        entity_id: "unknown_relation:edge_replay_replace",
        data: {
          diagnostic_id: "unknown_relation:edge_replay_replace",
          kind: "unknown_relation",
          edge_id: "edge_replay_replace",
          relation: "custom_old",
        },
      },
      {
        sequence: 3,
        time: "2026-07-14T06:00:02.000Z",
        run_id: "run_replay_replace",
        case_id: "case_replay_replace",
        operation: "edge.created",
        record_type: "edge",
        entity_id: "edge_replay_replace",
        data: relationEdge("edge_replay_replace", "produced"),
      },
    ])

    expect(replayed.edges).toEqual([
      expect.objectContaining({
        edge_id: "edge_replay_replace",
        original_relation: "produced",
        normalized_relation: "produced",
        eligible_for_attribution: true,
      }),
    ])
    expect(replayed.diagnostics).toEqual([])
  })

  test("canonicalizes lifecycle snapshots and derives diagnostics from their current edges", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T07:00:00.000Z",
        run_id: "run_lifecycle",
        case_id: "case_lifecycle",
        operation: "case.checkpointed",
        record_type: "checkpoint",
        entity_id: "case_lifecycle",
        data: {
          snapshot: {
            version: "1.0",
            runID: "run_lifecycle",
            caseID: "case_lifecycle",
            nodes: [],
            edges: [relationEdge("edge_lifecycle", "custom_lifecycle")],
            artifacts: [],
            diagnostics: [
              {
                diagnostic_id: "unknown_relation:edge_stale",
                kind: "unknown_relation",
                edge_id: "edge_stale",
                relation: "custom_stale",
              },
              { diagnostic_id: "diagnostic_retained", kind: "integrity_warning", message: "retain me" },
            ],
          },
          data: { phase: "checkpoint" },
        },
      },
    ])

    expect(replayed.edges[0]).toMatchObject({
      original_relation: "custom_lifecycle",
      normalized_relation: "derived_from",
      eligible_for_attribution: false,
    })
    expect(replayed.diagnostics).toEqual([
      { diagnostic_id: "diagnostic_retained", kind: "integrity_warning", message: "retain me" },
      expect.objectContaining({
        diagnostic_id: "unknown_relation:edge_lifecycle",
        edge_id: "edge_lifecycle",
        relation: "custom_lifecycle",
      }),
    ])
  })
})
