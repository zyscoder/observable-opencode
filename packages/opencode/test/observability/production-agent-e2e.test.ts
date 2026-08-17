import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createTask7ProductionHarness } from "./fixture/task-7-production-harness"

test(
  "production Agent executes tools and compaction, then resumes the same persisted session",
  async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task-7-production-agent-"))
    const harness = await createTask7ProductionHarness(root)
    try {
      const first = await harness.run({
        tracing: true,
        caseID: "task-7-production-agent",
        message: "TASK7_ROOT_WORKFLOW execute the deterministic production workflow",
      })
      expect(first.exitCode, first.stderr).toBe(0)
      expect(first.stdout).toContain("ROOT_WORKFLOW_COMPLETE")

      const sessions = harness.persistedSessions()
      expect(sessions).toHaveLength(2)
      const parent = sessions.find((row) => row.parent_id === null)
      const child = sessions.find((row) => row.parent_id === parent?.id)
      expect(parent?.id).toMatch(/^ses_/)
      expect(child?.id).toMatch(/^ses_/)

      const resumed = await harness.run({
        tracing: true,
        caseID: "task-7-production-agent",
        sessionID: parent!.id,
        message: "TASK7_RESUME_WORKFLOW confirm the same production session continues",
      })
      expect(resumed.exitCode, resumed.stderr).toBe(0)
      expect(resumed.stdout).toContain("RESUME_WORKFLOW_COMPLETE")

      const output = await fs.readFile(path.join(harness.projectDir, "agent-output.txt"), "utf8")
      expect(output).toBe("task-7-production-output\n")

      const messages = harness.persistedMessages(parent!.id)
      const toolParts = harness
        .persistedParts(parent!.id)
        .filter((row) => JSON.parse(row.data).type === "tool")
        .map((row) => JSON.parse(row.data))
      expect(messages.filter((row) => JSON.parse(row.data).role === "user")).toHaveLength(4)
      expect(toolParts.map((part) => part.tool)).toEqual(["write", "skill", "local_echo", "task"])
      expect(toolParts.every((part) => part.state.status === "completed")).toBe(true)

      const caseDir = path.join(harness.traceRoot, "task-7-production-agent")
      const session = JSON.parse(await fs.readFile(path.join(caseDir, "session.json"), "utf8")) as any
      const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
      const eventTypes = new Set(trace.records.map((record: any) => record.event_type))
      expect(session.session_id).toBe(parent!.id)
      expect(session.segments).toHaveLength(2)
      expect(session.segments[1].continuation_of).toBe(session.segments[0].run_id)
      for (const type of [
        "context.compaction",
        "tool.call",
        "tool.result",
        "skill.load",
        "mcp.call",
        "subagent.call",
        "agent.lifecycle",
        "response.output",
      ]) {
        expect(eventTypes.has(type), type).toBe(true)
      }
      expect(trace.edges.some((edge: any) => edge.normalized_relation === "continued_from")).toBe(true)
    } finally {
      await harness.dispose()
      await fs.rm(root, { recursive: true, force: true })
    }
  },
  1_200_000,
)
