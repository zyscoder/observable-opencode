import { describe, expect, test } from "bun:test"
import { CausalIRStore, replayCausalIRJournal } from "@/observability/causal-ir"

describe("causal IR store", () => {
  test("replays node creation and update into the same snapshot", () => {
    const journal: unknown[] = []
    const store = new CausalIRStore({ runID: "run_1", caseID: "case_1", append: (entry) => journal.push(entry) })
    const node = store.createNode({
      node_id: "node_1",
      kind: "decision",
      component: "task",
      timestamp: "2026-07-14T00:00:00.000Z",
      time_ms: 1,
      data: { chosen_action: "read" },
    })
    node.data = { chosen_action: "edit" }
    store.updateNode(node)

    expect(replayCausalIRJournal(journal).nodes).toEqual(store.snapshot().nodes)
    expect(journal.map((entry: any) => entry.operation)).toEqual(["node.created", "node.updated"])
  })
})
