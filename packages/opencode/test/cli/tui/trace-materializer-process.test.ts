import { expect, test } from "bun:test"
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { materializeWorkerTraces } from "@/cli/cmd/tui/trace-materializer-process"

test("materializes unique worker trace directories serially and continues after a child failure", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "opencode-trace-materializer-process-"))
  const fixture = path.join(dir, "fixture.ts")
  const first = path.join(dir, "first")
  const broken = path.join(dir, "broken")
  const second = path.join(dir, "second")

  try {
    await Promise.all([mkdir(first), mkdir(broken), mkdir(second)])
    await writeFile(
      fixture,
      [
        `import { appendFile, open, rm, writeFile } from "node:fs/promises"`,
        `import path from "node:path"`,
        `const [command, caseDir] = process.argv.slice(2)`,
        `const lock = path.join(process.env.OPENCODE_CASE_TRACE_DIR!, "materializer.lock")`,
        `try { await (await open(lock, "wx")).close() } catch { process.exit(8) }`,
        `await appendFile(path.join(process.env.OPENCODE_CASE_TRACE_DIR!, "materializer-events.jsonl"), JSON.stringify({ command, caseDir, trace: process.env.OPENCODE_CASE_TRACE, quiet: process.env.OPENCODE_CASE_TRACE_QUIET, unrelated: process.env.UNRELATED_SECRET }) + "\\n")`,
        `await Bun.sleep(50)`,
        `await rm(lock)`,
        `if (path.basename(caseDir) === "broken") process.exit(7)`,
        `await writeFile(path.join(caseDir, "trace.json"), "{}")`,
        `await writeFile(path.join(caseDir, "manifest.json"), JSON.stringify({ status: "success" }))`,
      ].join("\n"),
    )
    const warnings: string[] = []

    const publications = await materializeWorkerTraces(
      [
        { caseDir: first, caseID: "first", runID: "run", recordsFile: path.join(first, "records.jsonl") },
        { caseDir: broken, caseID: "broken", runID: "run", recordsFile: path.join(broken, "records.jsonl") },
        { caseDir: first, caseID: "first-duplicate", runID: "run", recordsFile: path.join(first, "records.jsonl") },
        { caseDir: second, caseID: "second", runID: "run", recordsFile: path.join(second, "records.jsonl") },
      ],
      {
        command: [process.execPath, fixture],
        env: {
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_TRACE_DIR: dir,
          OPENCODE_CASE_TRACE_QUIET: "1",
          UNRELATED_SECRET: "must-not-reach-child",
        },
        onWarning: (warning) => warnings.push(warning),
      },
    )

    const events = (await readFile(path.join(dir, "materializer-events.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line) as { command: string; caseDir: string; trace: string; quiet: string; unrelated?: string })

    expect(events.map((event) => path.basename(event.caseDir))).toEqual(["first", "broken", "second"])
    expect(events.map((event) => event.command)).toEqual(["trace-finalize", "trace-finalize", "trace-finalize"])
    expect(events).toEqual(expect.arrayContaining([expect.objectContaining({ trace: "1", quiet: "1" })]))
    expect(events.every((event) => !("unrelated" in event))).toBe(true)
    expect(warnings).toEqual([expect.stringContaining("exit 7")])
    expect(publications).toEqual([
      expect.objectContaining({ caseID: "first", traceFile: path.join(first, "trace.json") }),
      expect.objectContaining({ caseID: "second", traceFile: path.join(second, "trace.json") }),
    ])
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
