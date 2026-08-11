import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import * as CausalIRModule from "@/observability/causal-ir"
import {
  CausalIRStore,
  projectProvenanceTrace,
  replayCausalIRJournal,
  validateCausalIRJournal,
  type CausalIRJournalEntry,
  type CausalIRNodeInput,
} from "@/observability/causal-ir"
import type { ProvenanceRecord, TraceArtifact } from "@/observability/case-trace"
import {
  FORMAL_DATAFLOW_RELATIONS,
  FORMAL_RECORD_TYPES,
  RELATION_MIGRATIONS,
  normalizeRelationDetails,
} from "@/observability/trace-semantic-contract"

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
    from: { type: "external", id: "source_1" },
    to: { type: "external", id: "target_1" },
    relation: "produced",
  }
}

function relationEdge(edgeID: string, relation: string) {
  return {
    edge_id: edgeID,
    from: { type: "external", id: `${edgeID}_from` },
    to: { type: "external", id: `${edgeID}_to` },
    relation,
  }
}

test("external evaluation semantic contract preserves aliases and replay", () => {
  expect(FORMAL_RECORD_TYPES).toContain("external.evaluation_fact")
  expect(FORMAL_DATAFLOW_RELATIONS).toContain("external_evaluation_observed")
  expect(normalizeRelationDetails("external_evaluation_observed")).toEqual({
    original: "external_evaluation_observed",
    normalized: "external_evaluation_observed",
    known: true,
  })

  const journal: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({
    runID: "run_external_evaluation",
    caseID: "case_external_evaluation",
    append: (entry) => journal.push(entry),
  })
  store.createEdge({
    edge_id: "edge_external_evaluation",
    from: { type: "verification", id: "verification_1" },
    to: { type: "external_evaluation", id: "terminalbench_1" },
    relation: "external_evaluation_observed",
    eligible_for_attribution: true,
  })
  store.createNode({
    node_id: "external_evaluation_record_1",
    kind: "external.evaluation_fact",
    component: "evaluation",
    timestamp: "2026-07-21T12:00:00.000Z",
    time_ms: 1,
    status: "failed",
    data: {
      evaluation_id: "terminalbench_1",
      subject_revision: "git:abc123",
      revision_status: "matched",
      eligible_for_decisive_judgment: true,
      root_candidate_eligible: false,
    },
  })
  store.createNode({
    ...node("verification_record_1"),
    kind: "verification",
    data: { verification_id: "verification_1" },
  })

  const snapshot = store.snapshot()
  const evaluation = snapshot.nodes.find((item) => item.node_id === "external_evaluation_record_1")
  expect(evaluation?.aliases).toEqual(
    expect.arrayContaining([
      "external_evaluation:terminalbench_1",
      "external_evaluation:external_evaluation_record_1",
    ]),
  )
  expect(snapshot.edges[0]).toMatchObject({
    normalized_relation: "external_evaluation_observed",
    eligible_for_attribution: true,
    to: { ref_type: "node", ref_id: "external_evaluation_record_1" },
  })
  expect(replayCausalIRJournal(journal)).toEqual(snapshot)
})

function canonicalJSONForAudit(input: unknown, arrayValue = false): string | undefined {
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
    for (let index = 0; index < input.length; index++) values.push(canonicalJSONForAudit(input[index], true) ?? "null")
    return `[${values.join(",")}]`
  }
  const value = input as Record<string, unknown>
  if (typeof value.toJSON === "function") return canonicalJSONForAudit(value.toJSON(), arrayValue)
  return `{${Object.keys(value)
    .sort((left, right) => (left === right ? 0 : left < right ? -1 : 1))
    .flatMap((key) => {
      const serialized = canonicalJSONForAudit(value[key])
      return serialized === undefined ? [] : [`${JSON.stringify(key)}:${serialized}`]
    })
    .join(",")}}`
}

function payloadHashForAudit(input: unknown) {
  return createHash("sha256")
    .update(canonicalJSONForAudit(input) ?? "null")
    .digest("hex")
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

  test("replays claim group aliases and attribution-ineligible ordering without temporal causality", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_claim_group",
      caseID: "case_claim_group",
      append: (entry) => journal.push(entry),
    })
    const claimGroupData = {
      claim_group_id: "claim_group_1",
      claim_count: 2,
      source_byte_range: [0, 42],
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    }
    store.createNode({
      node_id: "responsenode_segment_1",
      kind: "response.output",
      component: "result",
      timestamp: "2026-07-22T00:00:00.000Z",
      time_ms: 1,
      data: { segment_id: "segment_1" },
    })
    store.createNode({
      node_id: "responseclaim_claim_1",
      kind: "response.claim",
      component: "result",
      timestamp: "2026-07-22T00:00:01.000Z",
      time_ms: 2,
      data: { ...claimGroupData, claim_id: "claim_1", next_claim_ref: "record:responseclaim_claim_2" },
      metadata: { ...claimGroupData, next_claim_ref: "record:responseclaim_claim_2" },
    })
    store.createNode({
      node_id: "responseclaim_claim_2",
      kind: "response.claim",
      component: "result",
      timestamp: "2026-07-22T00:00:02.000Z",
      time_ms: 3,
      data: { ...claimGroupData, claim_id: "claim_2", source_byte_range: [43, 84], previous_claim_ref: "record:responseclaim_claim_1" },
      metadata: { ...claimGroupData, source_byte_range: [43, 84], previous_claim_ref: "record:responseclaim_claim_1" },
    })
    store.createEdge({
      edge_id: "edge_response_claim_group_1",
      from: { type: "node", id: "responsenode_segment_1" },
      to: { type: "response_claim", id: "responseclaim_claim_1" },
      relation: "response_to_claim_group",
      metadata: { claim_group_id: "claim_group_1" },
    })
    store.createEdge({
      edge_id: "edge_claim_group_precedes_1",
      from: { type: "response_claim", id: "responseclaim_claim_1" },
      to: { type: "response_claim", id: "responseclaim_claim_2" },
      relation: "claim_group_precedes",
      eligible_for_attribution: false,
      metadata: {
        causal_semantics: "claim_group_order_only",
        eligible_for_attribution: false,
        behavior_impact: "none",
      },
    })

    const replayed = replayCausalIRJournal(journal)

    expect(replayed).toEqual(store.snapshot())
    expect(replayed.nodes.find((node) => node.node_id === "responseclaim_claim_1")).toMatchObject({
      aliases: expect.arrayContaining(["response_claim:claim_1"]),
      payload: { ...claimGroupData, claim_id: "claim_1", next_claim_ref: "record:responseclaim_claim_2" },
      metadata: { ...claimGroupData, next_claim_ref: "record:responseclaim_claim_2" },
    })
    expect(replayed.edges).toContainEqual(
      expect.objectContaining({
        original_relation: "claim_group_precedes",
        normalized_relation: "claim_group_precedes",
        evidence_tier: "confirmed",
        eligible_for_attribution: false,
        metadata: {
          causal_semantics: "claim_group_order_only",
          eligible_for_attribution: false,
          behavior_impact: "none",
        },
      }),
    )
    const projection = projectProvenanceTrace(replayed, {
      traceVersion: "6.0",
      manifest: { case_id: "case_claim_group", run_id: "run_claim_group" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })
    expect(projection.dataflow_edges.find((edge) => edge.relation === "claim_group_precedes")).toMatchObject({
      eligible_for_attribution: false,
      metadata: {
        causal_semantics: "claim_group_order_only",
        eligible_for_attribution: false,
        behavior_impact: "none",
      },
    })
    expect(replayed.edges.some((edge) => edge.evidence_tier === "temporal_advisory")).toBe(false)
    expect(replayed.diagnostics).toEqual([])
  })

  test("replays a node replacement journal prefix without a later lifecycle snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_nodes",
      caseID: "case_nodes",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("node_removed"))
    store.createNode(node("node_retained", { chosen_action: "keep" }))
    store.replaceNodes([node("node_replacement", { chosen_action: "replace" })])

    expect(journal.map((entry) => entry.operation)).toEqual(["node.created", "node.created", "case.checkpointed"])
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
    expect(replayCausalIRJournal(journal).nodes.map((item) => item.node_id)).toEqual(["node_replacement"])
  })

  test("replays an edge replacement journal prefix without a later lifecycle snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_edges",
      caseID: "case_edges",
      append: (entry) => journal.push(entry),
    })
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
    const store = new CausalIRStore({
      runID: "run_final",
      caseID: "case_final",
      append: (entry) => journal.push(entry),
    })
    const created = store.createNode(node("node_1", { chosen_action: "read" }))
    created.data = { chosen_action: "finalized" }
    store.finalize({ status: "success" })

    const finalizations = journal.filter((entry) => entry.operation === "case.finalized")
    expect(finalizations).toHaveLength(1)
    expect(replayCausalIRJournal(journal.slice(0, finalizations[0]!.sequence))).toEqual(store.snapshot())
  })

  test("replays the complete journal lifecycle with finalization as the last operation", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_lifecycle_complete",
      caseID: "case_lifecycle_complete",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("node_1"))
    store.createNode(node("node_2"))
    store.createEdge(edge("edge_1"))
    store.createArtifact({
      artifact_id: "artifact_1",
      hash: "hash_1",
      path: "artifacts/one.json",
      occurrences: 1,
    })
    store.createDiagnostic({ diagnostic_id: "diagnostic_1", kind: "integrity_warning", message: "retained" })
    store.checkpoint({ phase: "partial" })
    store.finalize({ status: "success" })

    expect(journal.filter((entry) => entry.operation === "case.checkpointed")).toHaveLength(1)
    expect(journal.filter((entry) => entry.operation === "case.finalized")).toHaveLength(1)
    expect(journal.at(-1)?.operation).toBe("case.finalized")
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
  })

  test("keeps finalization compact regardless of graph payload size", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_compact_final",
      caseID: "case_compact_final",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("large_node", { payload: "x".repeat(256 * 1024) }))
    store.checkpoint({ phase: "recoverable" })

    store.finalize({ status: "success", canonical_trace_path: "trace.json" })

    const finalization = journal.at(-1) as any
    expect(finalization.operation).toBe("case.finalized")
    expect(finalization.data.snapshot).toBeUndefined()
    expect(finalization.data.trace).toBeUndefined()
    expect(finalization.data.graph).toMatchObject({ nodes: 1, edges: 0, artifacts: 0, diagnostics: 0 })
    expect(Buffer.byteLength(JSON.stringify(finalization))).toBeLessThan(4096)
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
  })

  test("rebuilds node payload hashes from a replacement snapshot and clears removed node hashes", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_node_hash",
      caseID: "case_node_hash",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("node_1", { chosen_action: "original" }))
    const replacement = node("node_1", { chosen_action: "replacement" })
    store.replaceNodes([replacement])
    replacement.data = { chosen_action: "updated" }
    store.updateNode(replacement)

    const expectedReplacementHash = payloadHashForAudit((journal[1]?.data as any).snapshot.nodes[0])
    expect(journal[2]?.previous_payload_hash).toBe(expectedReplacementHash)
    expect(() => validateCausalIRJournal(journal)).not.toThrow()

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
    expect(() => validateCausalIRJournal(deletedJournal)).not.toThrow()
  })

  test("clears removed edge payload hashes after replacement", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_edge_hash",
      caseID: "case_edge_hash",
      append: (entry) => journal.push(entry),
    })
    store.createEdge(edge("edge_deleted"))
    store.replaceEdges([])
    store.createEdge(edge("edge_deleted"))

    expect(journal[2]?.previous_payload_hash).toBeUndefined()
    expect(() => validateCausalIRJournal(journal)).not.toThrow()
  })

  test("chains canonical payload hashes using locale-independent lexical key ordering", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({ runID: "run_hash", caseID: "case_hash", append: (entry) => journal.push(entry) })
    const created = store.createNode(node("node_1", { z: 0, ä: 2, a: 1 }))
    created.data = { ä: 2, a: 1, z: 3 }
    store.updateNode(created)

    const canonicalCreated = canonicalJSONForAudit(journal[0]?.data)
    const expectedCreatedHash = payloadHashForAudit(journal[0]?.data)
    expect(canonicalCreated).toContain('"payload":{"a":1,"z":0,"ä":2}')
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

    const canonicalCreated = canonicalJSONForAudit(journal[0]?.data)
    const expectedHash = payloadHashForAudit(journal[0]?.data)
    expect(canonicalCreated).toContain('"payload":{"10":"ten","2":"two","a":"letter"}')
    expect(journal[0]?.payload_hash).toBe(expectedHash)
  })

  test("hashes sparse array slots as JSON null values", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_sparse",
      caseID: "case_sparse",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("empty", { values: [] }))
    store.createNode(node("hole", { values: new Array(1) }))
    store.createNode(node("mixed", { values: [1, , 2] }))

    const expectedHoleHash = payloadHashForAudit(journal[1]?.data)
    const expectedMixedHash = payloadHashForAudit(journal[2]?.data)

    expect(journal[0]?.payload_hash).not.toBe(journal[1]?.payload_hash)
    expect(canonicalJSONForAudit((journal[1]?.data as any).payload.values)).toBe("[null]")
    expect(canonicalJSONForAudit((journal[2]?.data as any).payload.values)).toBe("[1,null,2]")
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
    const store = new CausalIRStore({
      runID: "run_sequence",
      caseID: "case_sequence",
      append: (entry) => journal.push(entry),
    })
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
    expect(journal[0]?.data).toMatchObject({
      node_id: "node_1",
      schema_version: "1.0",
      payload: { chosen_action: "read" },
      data: { chosen_action: "read" },
    })
  })

  test("emits the complete Causal IR 1.0 node and edge envelopes", () => {
    const store = new CausalIRStore({ runID: "run_envelope", caseID: "case_envelope" })
    store.createNode({
      ...node("node_envelope", { chosen_action: "inspect" }),
      span_id: "span_child",
      parent_span_id: "span_parent",
      input_refs: ["tool_call:call_1"],
      output_refs: ["tool_result:result_1"],
      source_refs: ["evidence:fact_1"],
      source_locations: [{ path: "src/index.ts", line_start: 4, line_end: 8 }],
      artifact_refs: ["artifact_1"],
    })
    store.createEdge({
      edge_id: "edge_envelope",
      from: { type: "tool_call", id: "call_1", label: "tool.call" },
      to: { type: "node", id: "node_envelope", label: "decision" },
      relation: "produced",
      evidence_tier: "content_matched",
      eligible_for_attribution: true,
      derivation_method: "explicit_test_fixture",
      evidence_refs: ["evidence:fact_1"],
      confidence: 0.9,
      metadata: { retained: true },
    })

    const snapshot = store.snapshot()
    expect(snapshot.nodes[0]).toMatchObject({
      node_id: "node_envelope",
      kind: "decision",
      schema_version: "1.0",
      origin: "observed",
      order: {
        sequence: 1,
        timestamp: "2026-07-14T00:00:00.000Z",
        time_ms: 1,
      },
      scope: {
        run_id: "run_envelope",
        case_id: "case_envelope",
        span_id: "span_child",
        parent_span_id: "span_parent",
      },
      payload: { chosen_action: "inspect" },
      input_refs: [{ ref_type: "external", ref_id: "call_1", legacy_ref: "tool_call:call_1" }],
      output_refs: [{ ref_type: "external", ref_id: "result_1", legacy_ref: "tool_result:result_1" }],
      source_refs: [{ ref_type: "external", ref_id: "fact_1", legacy_ref: "evidence:fact_1" }],
      source_locations: [{ path: "src/index.ts", line_start: 4, line_end: 8 }],
      artifact_refs: ["artifact_1"],
      aliases: expect.arrayContaining(["record:node_envelope", "node:node_envelope"]),
      derivation: null,
      integrity: {
        payload_hash: expect.stringMatching(/^[a-f0-9]{64}$/),
        source_hash: expect.stringMatching(/^[a-f0-9]{64}$/),
      },
    })
    expect(snapshot.edges[0]).toEqual({
      edge_id: "edge_envelope",
      from: { ref_type: "external", ref_id: "call_1", legacy_ref: "tool_call:call_1", label: "tool.call" },
      to: { ref_type: "node", ref_id: "node_envelope", legacy_ref: "node:node_envelope", label: "decision" },
      original_relation: "produced",
      normalized_relation: "produced",
      evidence_tier: "content_matched",
      eligible_for_attribution: true,
      derivation_method: "explicit_test_fixture",
      evidence_refs: [{ ref_type: "external", ref_id: "fact_1", legacy_ref: "evidence:fact_1" }],
      confidence: 0.9,
      metadata: { retained: true },
    })
  })

  test("strips temporal selectors recursively from canonical and compatibility ref fields", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_temporal_boundary",
      caseID: "case_temporal_boundary",
      append: (entry) => journal.push(entry),
    })
    store.createNode(node("source_node"))
    store.createNode({
      ...node("generic_temporal", {
        nested: {
          input_refs: ["recent_evidence_records", "node:source_node"],
          evidence_refs: ["recent_verification_records", "node:source_node"],
          payload_refs: ["recent_tool_results", "artifact:artifact_1"],
          compatibility_refs: {
            primary: "recent_evidence_records",
            concrete: "node:source_node",
          },
          typed_refs: [
            {
              ref_type: "external",
              ref_id: "recent_change_records",
              legacy_ref: "recent_change_records",
            },
            { ref_type: "node", ref_id: "source_node", legacy_ref: "node:source_node" },
          ],
        },
      }),
      input_refs: ["recent_evidence_records", "node:source_node"],
      output_refs: ["recent_change_records", "external:result"],
      source_refs: ["recent_tool_results"],
      metadata: {
        nested: {
          source_refs: ["recent_evidence_records", "node:source_node"],
        },
      },
    })
    store.createEdge({
      edge_id: "edge_temporal_boundary",
      from: { type: "node", id: "source_node" },
      to: { type: "node", id: "generic_temporal" },
      relation: "derived_from",
      evidence_refs: ["recent_evidence_records", "node:source_node"],
    })

    const snapshot = store.snapshot()
    const generic = snapshot.nodes.find((item) => item.node_id === "generic_temporal")!
    const canonicalEdge = snapshot.edges.find((item) => item.edge_id === "edge_temporal_boundary")!
    const projection = projectProvenanceTrace(snapshot, {
      traceVersion: "6.0",
      manifest: { case_id: "case_temporal_boundary", run_id: "run_temporal_boundary" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })
    const selectors = [
      "recent_evidence_records",
      "recent_verification_records",
      "recent_tool_results",
      "recent_change_records",
    ]

    for (const selector of selectors) {
      expect(JSON.stringify(snapshot)).not.toContain(selector)
      expect(JSON.stringify(journal)).not.toContain(selector)
      expect(JSON.stringify(projection)).not.toContain(selector)
    }
    expect(generic.input_refs).toEqual([{ ref_type: "node", ref_id: "source_node", legacy_ref: "node:source_node" }])
    expect(generic.output_refs).toEqual([{ ref_type: "external", ref_id: "result", legacy_ref: "external:result" }])
    expect(generic.source_refs).toEqual([])
    expect(generic.legacy_input_refs).toEqual(["node:source_node"])
    expect((generic.payload.nested as any).input_refs).toEqual(["node:source_node"])
    expect((generic.payload.nested as any).typed_refs).toEqual([
      { ref_type: "node", ref_id: "source_node", legacy_ref: "node:source_node" },
    ])
    expect((generic.payload.nested as any).compatibility_refs).toEqual({ concrete: "node:source_node" })
    expect(canonicalEdge.evidence_refs).toEqual([
      { ref_type: "node", ref_id: "source_node", legacy_ref: "node:source_node" },
    ])

    const rejected = new CausalIRStore({ runID: "run_temporal_endpoint", caseID: "case_temporal_endpoint" })
    expect(() =>
      rejected.createEdge({
        edge_id: "edge_temporal_endpoint",
        from: { type: "node", id: "recent_evidence_records" },
        to: { type: "external", id: "result" },
        relation: "derived_from",
      }),
    ).toThrow(/temporal selector/)
    expect(rejected.edges).toEqual([])
  })

  test("resolves legacy endpoint aliases to distinct canonical node identities", () => {
    const store = new CausalIRStore({ runID: "run_alias", caseID: "case_alias" })
    store.createNode({
      ...node("toolcall_shared"),
      kind: "tool.call",
      component: "tool",
      data: { call_id: "shared_call" },
    })
    store.createNode({
      ...node("toolresult_shared"),
      kind: "tool.result",
      component: "tool",
      data: { call_id: "shared_call" },
    })
    store.createEdge({
      edge_id: "edge_shared_call",
      from: { type: "tool_call", id: "shared_call" },
      to: { type: "tool_result", id: "shared_call" },
      relation: "produced",
    })
    store.createEdge({
      edge_id: "edge_unresolved",
      from: { type: "verification", id: "missing_verification" },
      to: { type: "tool_result", id: "shared_call" },
      relation: "derived_from",
    })
    store.createEdge({
      edge_id: "edge_explicit_node_unresolved",
      from: { type: "node", id: "missing_node" },
      to: { type: "tool_result", id: "shared_call" },
      relation: "derived_from",
    })

    const snapshot = store.snapshot()
    const call = snapshot.nodes.find((item) => item.node_id === "toolcall_shared")
    const result = snapshot.nodes.find((item) => item.node_id === "toolresult_shared")
    const resolved = snapshot.edges.find((item) => item.edge_id === "edge_shared_call")
    const unresolved = snapshot.edges.find((item) => item.edge_id === "edge_unresolved")
    const explicitNodeUnresolved = snapshot.edges.find((item) => item.edge_id === "edge_explicit_node_unresolved")

    expect(call?.aliases).toContain("tool_call:shared_call")
    expect(result?.aliases).toContain("tool_result:shared_call")
    expect(resolved).toMatchObject({
      from: { ref_type: "node", ref_id: "toolcall_shared", legacy_ref: "tool_call:shared_call" },
      to: { ref_type: "node", ref_id: "toolresult_shared", legacy_ref: "tool_result:shared_call" },
    })
    expect(resolved?.from.ref_id).not.toBe(resolved?.to.ref_id)
    expect(unresolved?.from).toEqual({
      ref_type: "external",
      ref_id: "missing_verification",
      legacy_ref: "verification:missing_verification",
    })
    expect(explicitNodeUnresolved?.from).toEqual({
      ref_type: "external",
      ref_id: "missing_node",
      legacy_ref: "node:missing_node",
    })
    expect(snapshot.diagnostics).toContainEqual(
      expect.objectContaining({
        diagnostic_id: expect.stringMatching(/^unresolved_ref:[a-f0-9]{16}$/),
        kind: "unresolved_ref",
        edge_id: "edge_unresolved",
        field: "from",
        legacy_ref: "verification:missing_verification",
        occurrence_count: 1,
      }),
    )
    expect(store.snapshot().diagnostics).toEqual(snapshot.diagnostics)

    const projection = projectProvenanceTrace(snapshot, {
      traceVersion: "6.0",
      manifest: { case_id: "case_alias", run_id: "run_alias" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })
    expect(projection.dataflow_edges.find((item) => item.edge_id === "edge_shared_call")).toMatchObject({
      from: { type: "tool_call", id: "shared_call" },
      to: { type: "tool_result", id: "shared_call" },
    })
  })

  test("resolves context snapshots and tool spans while preserving declared external identities", () => {
    const store = new CausalIRStore({ runID: "run_runtime_aliases", caseID: "case_runtime_aliases" })
    store.createNode({
      ...node("context_pack_1"),
      kind: "context.pack",
      component: "context",
      data: { snapshot_id: "ctx_1" },
    })
    store.createNode({
      ...node("tool_call_1"),
      kind: "tool.call",
      component: "tool",
      span_id: "span_1",
      data: { call_id: "call_1" },
    })
    store.createEdge({
      edge_id: "edge_context_snapshot",
      from: { type: "context_snapshot", id: "ctx_1" },
      to: { type: "tool_span", id: "span_1" },
      relation: "used_as_context",
    })
    store.createEdge({
      edge_id: "edge_span_alias",
      from: { type: "span", id: "span_1" },
      to: { type: "external", id: "result" },
      relation: "produced",
    })
    store.createEdge({
      edge_id: "edge_declared_external",
      from: { type: "session", id: "ses_1" },
      to: { type: "message", id: "msg_1" },
      relation: "continued_from",
      evidence_refs: ["processor_text:txt_1"],
    })

    const snapshot = store.snapshot()
    expect(snapshot.nodes.find((item) => item.node_id === "context_pack_1")?.aliases).toContain(
      "context_snapshot:ctx_1",
    )
    expect(snapshot.nodes.find((item) => item.node_id === "tool_call_1")?.aliases).toEqual(
      expect.arrayContaining(["tool_span:span_1", "span:span_1"]),
    )
    expect(snapshot.edges.find((item) => item.edge_id === "edge_context_snapshot")).toMatchObject({
      from: { ref_type: "node", ref_id: "context_pack_1" },
      to: { ref_type: "node", ref_id: "tool_call_1" },
    })
    expect(snapshot.edges.find((item) => item.edge_id === "edge_span_alias")?.from).toMatchObject({
      ref_type: "node",
      ref_id: "tool_call_1",
    })
    expect(snapshot.edges.find((item) => item.edge_id === "edge_declared_external")).toMatchObject({
      from: { ref_type: "external", ref_id: "ses_1" },
      to: { ref_type: "external", ref_id: "msg_1" },
      evidence_refs: [{ ref_type: "external", ref_id: "txt_1" }],
    })
    expect(snapshot.diagnostics.filter((item) => item.kind === "unresolved_ref")).toEqual([])
  })

  test("aggregates repeated unresolved references by legacy identity", () => {
    const store = new CausalIRStore({ runID: "run_unresolved_aggregate", caseID: "case_unresolved_aggregate" })
    store.createEdge({
      edge_id: "edge_missing_1",
      from: { type: "verification", id: "missing_verification" },
      to: { type: "external", id: "result_1" },
      relation: "derived_from",
    })
    store.createEdge({
      edge_id: "edge_missing_2",
      from: { type: "verification", id: "missing_verification" },
      to: { type: "external", id: "result_2" },
      relation: "derived_from",
    })

    const unresolved = store.snapshot().diagnostics.filter((item) => item.kind === "unresolved_ref")
    expect(unresolved).toHaveLength(1)
    expect(unresolved[0]).toMatchObject({
      legacy_ref: "verification:missing_verification",
      occurrence_count: 2,
      affected_owners: [
        { owner_type: "edge", owner_id: "edge_missing_1", field: "from" },
        { owner_type: "edge", owner_id: "edge_missing_2", field: "from" },
      ],
    })
  })

  test("journals late alias reconciliation so every durable prefix replays the live snapshot", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_late_alias",
      caseID: "case_late_alias",
      append: (entry) => journal.push(entry),
    })
    store.createEdge({
      edge_id: "edge_late_alias",
      from: { type: "verification", id: "late_verification" },
      to: { type: "external", id: "result" },
      relation: "derived_from",
    })
    const originalEdgeEntry = journal.find((entry) => entry.operation === "edge.created")!
    const beforeResolution = journal.length

    store.createNode({
      ...node("verification_node"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "late_verification" },
    })

    const snapshot = store.snapshot()
    const reconciliation = journal.slice(beforeResolution)
    const reconciledEdgeEntry = reconciliation.find(
      (entry) => entry.operation === "edge.created" && entry.entity_id === "edge_late_alias",
    )
    expect(snapshot.edges[0]?.from).toEqual({
      ref_type: "node",
      ref_id: "verification_node",
      legacy_ref: "verification:late_verification",
    })
    expect(snapshot.diagnostics.some((item) => item.kind === "unresolved_ref")).toBe(false)
    expect(reconciledEdgeEntry).toMatchObject({
      operation: "edge.created",
      record_type: "edge",
      previous_payload_hash: originalEdgeEntry.payload_hash,
    })
    expect(reconciledEdgeEntry?.payload_hash).toMatch(/^[a-f0-9]{64}$/)
    expect(replayCausalIRJournal(journal)).toEqual(snapshot)

    const recordTypes: Record<CausalIRJournalEntry["operation"], string> = {
      "node.created": "node",
      "node.updated": "node.update",
      "edge.created": "edge",
      "artifact.created": "artifact",
      "artifact.reused": "artifact.reuse",
      "diagnostic.created": "diagnostic",
      "case.checkpointed": "checkpoint",
      "case.finalized": "finish",
    }
    for (const [index, entry] of journal.entries()) {
      expect(entry.sequence).toBe(index + 1)
      expect(entry.record_type).toBe(recordTypes[entry.operation])
      expect(entry.payload_hash).toBe(payloadHashForAudit(entry.data))
    }
  })

  test("chains alias-reconciled edge hashes through node replacement checkpoints", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_alias_checkpoint_hash",
      caseID: "case_alias_checkpoint_hash",
      append: (entry) => journal.push(entry),
    })
    store.createEdge({
      edge_id: "edge_alias_checkpoint_hash",
      from: { type: "verification", id: "checkpoint_verification" },
      to: { type: "external", id: "result" },
      relation: "derived_from",
    })
    store.replaceNodes([
      {
        ...node("checkpoint_owner"),
        kind: "verification",
        component: "tool",
        data: { verification_id: "checkpoint_verification" },
      },
    ])
    const checkpoint = journal.find((entry) => entry.operation === "case.checkpointed")!
    const checkpointEdge = (checkpoint.data as any).snapshot.edges[0]

    store.updateNode({
      ...node("checkpoint_owner"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "replacement_verification" },
    })

    const reconciledEdge = journal
      .filter((entry) => entry.operation === "edge.created" && entry.entity_id === "edge_alias_checkpoint_hash")
      .at(-1)!
    expect(reconciledEdge.previous_payload_hash).toBe(payloadHashForAudit(checkpointEdge))
    expect(replayCausalIRJournal(journal)).toEqual(store.snapshot())
  })

  test("keeps collided aliases external until one durable owner remains", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_alias_collision",
      caseID: "case_alias_collision",
      append: (entry) => journal.push(entry),
    })
    store.createNode({
      ...node("verification_owner_a"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "shared_verification" },
    })
    store.createNode({
      ...node("verification_owner_b"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "shared_verification" },
    })
    store.createEdge({
      edge_id: "edge_ambiguous_alias",
      from: { type: "verification", id: "shared_verification" },
      to: { type: "external", id: "result" },
      relation: "derived_from",
    })

    const ambiguous = store.snapshot()
    const collision = ambiguous.diagnostics.filter((item) => item.kind === "alias_collision")
    expect(ambiguous.edges[0]?.from).toEqual({
      ref_type: "external",
      ref_id: "shared_verification",
      legacy_ref: "verification:shared_verification",
    })
    expect(collision).toEqual([
      expect.objectContaining({
        diagnostic_id: expect.stringMatching(/^alias_collision:/),
        alias: "verification:shared_verification",
        owner_ids: ["verification_owner_a", "verification_owner_b"],
      }),
    ])
    expect(ambiguous.diagnostics).toContainEqual(
      expect.objectContaining({ kind: "unresolved_ref", edge_id: "edge_ambiguous_alias" }),
    )
    expect(store.snapshot().diagnostics.filter((item) => item.kind === "alias_collision")).toEqual(collision)

    store.updateNode({
      ...node("verification_owner_b"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "unique_verification" },
    })

    const resolved = store.snapshot()
    expect(resolved.edges[0]?.from).toEqual({
      ref_type: "node",
      ref_id: "verification_owner_a",
      legacy_ref: "verification:shared_verification",
    })
    expect(resolved.diagnostics.some((item) => item.kind === "alias_collision")).toBe(false)
    expect(resolved.diagnostics.some((item) => item.kind === "unresolved_ref")).toBe(false)
    expect(
      journal.some((entry) => entry.operation === "edge.created" && entry.entity_id === "edge_ambiguous_alias"),
    ).toBe(true)
    expect(replayCausalIRJournal(journal)).toEqual(resolved)
  })

  test("rejects invalid derived provenance and accepts typed reproducible inputs", () => {
    const store = new CausalIRStore({ runID: "run_derivation", caseID: "case_derivation" })
    store.createNode(node("observed_input"))
    const derivedBase = {
      ...node("derived_output"),
      origin: "deterministic_derived" as const,
      input_refs: ["node:observed_input"],
    }

    expect(() => store.createNode(derivedBase)).toThrow("derived node requires derivation provenance")
    expect(() =>
      store.createNode({
        ...derivedBase,
        derivation: {
          algorithm: "fixture_derivation",
          algorithm_version: "1.0.0",
          derived_at: "2026-07-15T00:00:00.000Z",
          input_refs: [],
          reproducible: true,
        },
      }),
    ).toThrow("derived node requires non-empty typed derivation input refs")
    expect(() =>
      store.createNode({
        ...node("observed_with_derivation"),
        derivation: {
          algorithm: "invalid_observed_derivation",
          algorithm_version: "1.0.0",
          derived_at: "2026-07-15T00:00:00.000Z",
          input_refs: [{ ref_type: "node", ref_id: "observed_input" }],
          reproducible: true,
        },
      }),
    ).toThrow("observed node cannot declare derivation provenance")

    store.createNode({
      ...derivedBase,
      derivation: {
        algorithm: "fixture_derivation",
        algorithm_version: "1.0.0",
        derived_at: "2026-07-15T00:00:00.000Z",
        input_refs: [{ ref_type: "node", ref_id: "observed_input", legacy_ref: "node:observed_input" }],
        reproducible: true,
      },
    })
    expect(store.snapshot().nodes.find((item) => item.node_id === "derived_output")).toMatchObject({
      origin: "deterministic_derived",
      input_refs: [{ ref_type: "node", ref_id: "observed_input", legacy_ref: "node:observed_input" }],
      derivation: {
        algorithm: "fixture_derivation",
        algorithm_version: "1.0.0",
        input_refs: [{ ref_type: "node", ref_id: "observed_input", legacy_ref: "node:observed_input" }],
        reproducible: true,
      },
    })
  })

  test("requires canonically equal non-empty and valid derived input ref sets", () => {
    const makeStore = () => {
      const store = new CausalIRStore({ runID: "run_strict_derivation", caseID: "case_strict_derivation" })
      store.createNode(node("observed_input"))
      return store
    }
    const derived = (
      nodeID: string,
      inputRefs: string[],
      derivationRefs: Array<{
        ref_type: "node" | "artifact" | "external"
        ref_id: string
        legacy_ref?: string
      }>,
    ) => ({
      ...node(nodeID),
      origin: "deterministic_derived" as const,
      input_refs: inputRefs,
      derivation: {
        algorithm: "strict_fixture",
        algorithm_version: "1.0.0",
        derived_at: "2026-07-15T00:00:00.000Z",
        input_refs: derivationRefs,
        reproducible: true,
      },
    })

    expect(() =>
      makeStore().createNode(derived("derived_empty_input", [""], [{ ref_type: "external", ref_id: "contract" }])),
    ).toThrow(/valid non-empty input refs/)
    expect(() =>
      makeStore().createNode(
        derived("derived_mismatch", ["node:observed_input"], [{ ref_type: "external", ref_id: "different_input" }]),
      ),
    ).toThrow(/canonically match/)
    expect(() =>
      makeStore().createNode(
        derived(
          "derived_missing_node",
          ["node:missing_input"],
          [{ ref_type: "node", ref_id: "missing_input", legacy_ref: "node:missing_input" }],
        ),
      ),
    ).toThrow(/existing node/)
    expect(() =>
      makeStore().createNode(
        derived(
          "derived_missing_artifact",
          ["artifact:missing_artifact"],
          [{ ref_type: "artifact", ref_id: "missing_artifact", legacy_ref: "artifact:missing_artifact" }],
        ),
      ),
    ).toThrow(/existing artifact/)

    const store = makeStore()
    store.createArtifact({ artifact_id: "artifact_derived", hash: "hash_derived", path: "artifact.json" })
    store.createNode(
      derived(
        "derived_valid",
        ["decision:observed_input", "decision:observed_input", "artifact:artifact_derived", "external:contract"],
        [
          { ref_type: "node", ref_id: "observed_input", legacy_ref: "node:observed_input" },
          { ref_type: "artifact", ref_id: "artifact_derived", legacy_ref: "artifact:artifact_derived" },
          { ref_type: "external", ref_id: "contract", legacy_ref: "external:contract" },
        ],
      ),
    )
    const valid = store.snapshot().nodes.find((item) => item.node_id === "derived_valid")!
    expect(valid.input_refs).toEqual(valid.derivation!.input_refs)
    expect(valid.input_refs).toHaveLength(3)
  })

  test("validates replacement-derived refs against the replacement graph", () => {
    const derived = (nodeID: string, inputID: string) => ({
      ...node(nodeID),
      origin: "deterministic_derived" as const,
      input_refs: [`node:${inputID}`],
      derivation: {
        algorithm: "replacement_fixture",
        algorithm_version: "1.0.0",
        derived_at: "2026-07-15T00:00:00.000Z",
        input_refs: [{ ref_type: "node" as const, ref_id: inputID, legacy_ref: `node:${inputID}` }],
        reproducible: true,
      },
    })
    const store = new CausalIRStore({ runID: "run_replace_validation", caseID: "case_replace_validation" })
    store.createNode(node("old_input"))

    expect(() =>
      store.replaceNodes([node("replacement_input"), derived("replacement_output", "replacement_input")]),
    ).not.toThrow()
    expect(store.snapshot().nodes.map((item) => item.node_id)).toEqual(["replacement_input", "replacement_output"])

    const staleStore = new CausalIRStore({ runID: "run_replace_stale", caseID: "case_replace_stale" })
    staleStore.createNode(node("old_input"))
    expect(() => staleStore.replaceNodes([derived("stale_output", "old_input")])).toThrow(/existing node/)
  })

  test("poisons journal appends without advancing committed sequence or hashes", () => {
    const persisted: CausalIRJournalEntry[] = []
    let attempts = 0
    const store = new CausalIRStore({
      runID: "run_poison",
      caseID: "case_poison",
      append: (entry) => {
        attempts += 1
        if (attempts === 2) return false
        persisted.push(structuredClone(entry))
        return true
      },
    })
    const first = node("node_first", { chosen_action: "first" })
    const failed = node("node_failed", { chosen_action: "failed append" })
    const afterFailure = node("node_after_failure", { chosen_action: "must not append" })

    store.createNode(first)
    store.createNode(failed)
    store.createNode(afterFailure)

    expect(persisted.map((entry) => entry.sequence)).toEqual([1])
    expect(attempts).toBe(2)
    expect(store.nodes).toEqual([first, failed, afterFailure])
    expect((store as any).journalSummary()).toMatchObject({
      entry_count: 1,
      last_sequence: 1,
      last_payload_hash: persisted[0]?.payload_hash,
      poisoned: true,
    })
  })

  test("returns the durable commit result when finalization append fails", () => {
    const journal: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_finalize_failure",
      caseID: "case_finalize_failure",
      append: (entry) => {
        if (entry.operation === "case.finalized") return false
        journal.push(entry)
        return true
      },
    })
    store.createNode(node("node_before_finalize_failure"))
    const before = store.journalSummary()

    const result = store.finalize({ status: "success" }) as any

    expect(result).toEqual({
      committed: false,
      operation: "case.finalized",
      sequence: before.last_sequence,
      payload_hash: undefined,
      poisoned: true,
    })
    expect(store.journalSummary()).toMatchObject({
      entry_count: before.entry_count,
      last_sequence: before.last_sequence,
      last_payload_hash: before.last_payload_hash,
      poisoned: true,
    })
    expect(journal.some((entry) => entry.operation === "case.finalized")).toBe(false)
  })

  test("inserts an edge without rescanning existing edges or diagnostics", () => {
    const store = new CausalIRStore({ runID: "run_linear", caseID: "case_linear" })
    for (let index = 0; index < 128; index++) store.createEdge(relationEdge(`edge_${index}`, "produced"))

    let existingEdgeReads = 0
    for (let index = 0; index < store.edges.length; index++) {
      store.edges[index] = new Proxy(store.edges[index]!, {
        get(target, property, receiver) {
          if (property === "edge_id" || property === "relation" || property === "original_relation") {
            existingEdgeReads += 1
          }
          return Reflect.get(target, property, receiver)
        },
      })
    }

    store.createEdge(relationEdge("edge_new", "produced"))

    expect(existingEdgeReads).toBeLessThan(8)
  })

  test("creates and updates nodes without scanning unrelated nodes edges or diagnostics", () => {
    const store = new CausalIRStore({ runID: "run_incremental_alias", caseID: "case_incremental_alias" })
    for (let index = 0; index < 64; index++) store.createNode(node(`existing_node_${index}`))
    for (let index = 0; index < 64; index++) {
      store.createEdge({
        edge_id: `unresolved_edge_${index}`,
        from: { type: "verification", id: `late_alias_${index}` },
        to: { type: "external", id: `target_${index}` },
        relation: "derived_from",
      })
    }

    let nodeReads = 0
    let edgeReads = 0
    let diagnosticReads = 0
    for (let index = 0; index < store.nodes.length; index++) {
      store.nodes[index] = new Proxy(store.nodes[index]!, {
        get(target, property, receiver) {
          nodeReads += 1
          return Reflect.get(target, property, receiver)
        },
      })
    }
    for (let index = 0; index < store.edges.length; index++) {
      store.edges[index] = new Proxy(store.edges[index]!, {
        get(target, property, receiver) {
          edgeReads += 1
          return Reflect.get(target, property, receiver)
        },
      })
    }
    for (let index = 0; index < store.diagnostics.length; index++) {
      store.diagnostics[index] = new Proxy(store.diagnostics[index]!, {
        get(target, property, receiver) {
          diagnosticReads += 1
          return Reflect.get(target, property, receiver)
        },
      })
    }

    store.createNode({
      ...node("late_owner"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "late_alias_31" },
    })

    expect(nodeReads).toBeLessThan(20)
    expect(edgeReads).toBeLessThan(48)
    expect(diagnosticReads).toBeLessThan(48)

    nodeReads = 0
    edgeReads = 0
    diagnosticReads = 0
    store.updateNode({
      ...node("late_owner"),
      kind: "verification",
      component: "tool",
      data: { verification_id: "late_alias_unique" },
    })

    expect(nodeReads).toBeLessThan(20)
    expect(edgeReads).toBeLessThan(48)
    expect(diagnosticReads).toBeLessThan(48)
  })

  test("exports a full canonical trace replay API", () => {
    expect(typeof (CausalIRModule as any).replayCausalIRTrace).toBe("function")
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
      normalized_relation: "derived_from",
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
        total: 8,
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
      preview: '{"ok":true}',
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
      from: { type: "external", id: "source_1" },
      to: { type: "external", id: "target_1" },
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
      from: { type: "external", id: "source_1" },
      to: { type: "external", id: "target_1" },
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
      from: { type: "external", id: "source_1" },
      to: { type: "external", id: "target_1" },
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

  test("last write wins when replaceEdges receives duplicate edge IDs", () => {
    const store = new CausalIRStore({ runID: "run_replace_duplicates", caseID: "case_replace_duplicates" })

    store.replaceEdges([relationEdge("edge_duplicate", "custom_first"), relationEdge("edge_duplicate", "custom_last")])

    expect(store.edges).toEqual([
      expect.objectContaining({
        edge_id: "edge_duplicate",
        original_relation: "custom_last",
        normalized_relation: "derived_from",
      }),
    ])
    expect(store.diagnostics).toEqual([
      expect.objectContaining({
        diagnostic_id: "unknown_relation:edge_duplicate",
        relation: "custom_last",
      }),
    ])
  })

  test("last write wins for duplicate edge IDs in lifecycle snapshots", () => {
    const replayed = replayCausalIRJournal([
      {
        sequence: 1,
        time: "2026-07-14T08:00:00.000Z",
        run_id: "run_lifecycle_duplicates",
        case_id: "case_lifecycle_duplicates",
        operation: "case.checkpointed",
        record_type: "checkpoint",
        entity_id: "case_lifecycle_duplicates",
        data: {
          snapshot: {
            version: "1.0",
            runID: "run_lifecycle_duplicates",
            caseID: "case_lifecycle_duplicates",
            nodes: [],
            edges: [relationEdge("edge_duplicate", "custom_first"), relationEdge("edge_duplicate", "custom_last")],
            artifacts: [],
            diagnostics: [],
          },
          data: { phase: "checkpoint" },
        },
      },
    ])

    expect(replayed.edges).toEqual([
      expect.objectContaining({
        edge_id: "edge_duplicate",
        original_relation: "custom_last",
        normalized_relation: "derived_from",
      }),
    ])
    expect(replayed.diagnostics).toEqual([
      expect.objectContaining({
        diagnostic_id: "unknown_relation:edge_duplicate",
        relation: "custom_last",
      }),
    ])
  })

  test("overwrites reserved projected relation metadata with canonical values", () => {
    const store = new CausalIRStore({ runID: "run_projection_metadata", caseID: "case_projection_metadata" })
    store.createEdge({
      ...relationEdge("edge_projection_metadata", "custom_relation"),
      metadata: {
        retained: true,
        normalized_relation: "produced",
        original_relation: "forged_original",
        evidence_tier: "content_matched",
        eligible_for_attribution: true,
        derivation_method: "forged_method",
      },
    })

    const projection = projectProvenanceTrace(store.snapshot(), {
      traceVersion: "5.6",
      manifest: { case_id: "case_projection_metadata", run_id: "run_projection_metadata" },
      metrics: { token_usage: {}, trace_health: { issues: [] } },
    })

    expect(projection.dataflow_edges[0]).toMatchObject({
      relation: "derived_from",
      metadata: {
        retained: true,
        original_relation: "custom_relation",
        normalized_relation: "derived_from",
        evidence_tier: "content_matched",
        eligible_for_attribution: false,
        derivation_method: "forged_method",
      },
    })
  })
})
