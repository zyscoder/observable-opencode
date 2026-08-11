import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const cli = path.resolve(import.meta.dir, "../src/cli.ts")

function sha256(content: Uint8Array) {
  return createHash("sha256").update(content).digest("hex")
}

async function hashes(caseDir: string) {
  const files = await fs.readdir(caseDir, { recursive: true, withFileTypes: true })
  const targets = files
    .filter((entry) => entry.isFile())
    .map((entry) => path.join(entry.parentPath, entry.name))
    .filter((file) => file.endsWith(".json") || file.endsWith(".jsonl") || file.includes(`${path.sep}artifacts${path.sep}`))
    .sort()
  return new Map(await Promise.all(targets.map(async (file) => [file, sha256(await fs.readFile(file))] as const)))
}

function run(...args: string[]) {
  return runCommand(process.execPath, cli, ...args)
}

function runCommand(command: string, ...args: string[]) {
  return Bun.spawnSync({
    cmd: [command, ...args],
    stdout: "pipe",
    stderr: "pipe",
  })
}

async function withCaseDirectory(runTest: (caseDir: string) => Promise<void>) {
  const caseDir = await fs.mkdtemp(path.join(os.tmpdir(), "observable-trace-cli-"))
  try {
    await runTest(caseDir)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
}

async function writeFinalizedCase(caseDir: string) {
  await fs.mkdir(path.join(caseDir, "artifacts"), { recursive: true })
  await fs.writeFile(path.join(caseDir, "artifacts", "input.txt"), "authoritative artifact payload")
  await fs.writeFile(path.join(caseDir, "records.jsonl"), '{"preserved":"journal input"}\n')
  await fs.writeFile(
    path.join(caseDir, "trace.json"),
    JSON.stringify({
      trace_schema_version: "6.0",
      case_id: "finalized-cli-case",
      run_id: "run_finalized_cli",
      records: [
        {
          record_id: "record_1",
          component: "result",
          event_type: "response.output",
          timestamp: "2026-08-11T00:00:00.000Z",
          time_ms: 1,
          title: "finalized record",
          data: { text: "semantic payload must not be printed" },
        },
      ],
      dataflow_edges: [],
      artifacts: [
        {
          artifact_id: "input_artifact",
          kind: "text",
          label: "input",
          path: "artifacts/input.txt",
          length: 30,
          hash: "input-hash",
          preview: "authoritative artifact payload",
          created_at: "2026-08-11T00:00:00.000Z",
          occurrences: 1,
          availability: "bundled",
        },
      ],
    }),
  )
}

describe("observable-trace render", () => {
  test("renders a finalized case to the default output without mutating inputs", async () => {
    await withCaseDirectory(async (caseDir) => {
      await writeFinalizedCase(caseDir)
      const before = await hashes(caseDir)

      const result = run("render", caseDir)
      const output = path.join(caseDir, "trace.html")
      const after = await hashes(caseDir)

      expect(result.exitCode).toBe(0)
      expect(Buffer.from(result.stdout).toString()).toContain("source: trace.json")
      expect(Buffer.from(result.stdout).toString()).toContain("completeness: complete")
      expect(Buffer.from(result.stdout).toString()).toContain(`output: ${output}`)
      expect(Buffer.from(result.stdout).toString()).not.toContain("semantic payload must not be printed")
      expect((await fs.stat(output)).isFile()).toBe(true)
      expect(before).toEqual(new Map([...after].filter(([file]) => before.has(file))))
      expect([...after.keys()].filter((file) => !before.has(file))).toEqual([
        path.join(caseDir, "artifacts", "render-snapshots", "sha256", sha256(Buffer.from("authoritative artifact payload"))),
      ])
    })
  })

  test("renders an incomplete journal with a visible recovery banner", async () => {
    await withCaseDirectory(async (caseDir) => {
      await fs.writeFile(
        path.join(caseDir, "records.jsonl"),
        `${JSON.stringify({
          sequence: 1,
          time: "2026-08-11T00:00:00.000Z",
          run_id: "run_journal_recovery",
          case_id: "journal-recovery-case",
          operation: "node.created",
          record_type: "node",
          entity_id: "response_1",
          data: {
            node_id: "response_1",
            kind: "response.output",
            component: "result",
            timestamp: "2026-08-11T00:00:00.000Z",
            time_ms: 1,
            status: "cancelled",
            data: { text: "recovered journal payload" },
          },
        })}\n`,
      )
      const before = await hashes(caseDir)

      const result = run("render", path.join(caseDir, "records.jsonl"))
      const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
      const after = await hashes(caseDir)

      expect(result.exitCode).toBe(0)
      expect(Buffer.from(result.stdout).toString()).toContain("source: records.jsonl")
      expect(Buffer.from(result.stdout).toString()).toContain("completeness: incomplete")
      expect(html).toContain("Incomplete journal recovery")
      expect(html).toContain("incomplete_journal_replay")
      expect(after).toEqual(before)
    })
  })

  test("writes to an explicitly requested absolute output path", async () => {
    await withCaseDirectory(async (caseDir) => {
      await writeFinalizedCase(caseDir)
      const output = path.join(caseDir, "reports", "rendered.html")

      const result = run("render", caseDir, "--output", path.relative(process.cwd(), output))

      expect(result.exitCode).toBe(0)
      expect(Buffer.from(result.stdout).toString()).toContain(`output: ${output}`)
      expect(await fs.readFile(output, "utf8")).toContain("Trace v6.0")
      expect(await fs.stat(path.join(caseDir, "trace.html")).catch(() => undefined)).toBeUndefined()
    })
  })

  test("publishes external-output artifact snapshots from the loaded case", async () => {
    await withCaseDirectory(async (caseDir) => {
      const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), "observable-trace-external-output-"))
      try {
        await writeFinalizedCase(caseDir)
        const before = await hashes(caseDir)
        const output = path.join(outputDir, "trace.html")
        const snapshot = path.join(
          outputDir,
          "artifacts",
          "render-snapshots",
          "sha256",
          sha256(Buffer.from("authoritative artifact payload")),
        )

        const result = run("render", caseDir, "--output", output)
        const html = await fs.readFile(output, "utf8")
        const after = await hashes(caseDir)

        expect(result.exitCode).toBe(0)
        expect(html).toContain(`href="artifacts/render-snapshots/sha256/${path.basename(snapshot)}"`)
        expect(await fs.readFile(snapshot, "utf8")).toBe("authoritative artifact payload")
        expect(after).toEqual(before)
      } finally {
        await fs.rm(outputDir, { recursive: true, force: true })
      }
    })
  })

  test("rejects an output artifacts symlink without publishing HTML or snapshots", async () => {
    await withCaseDirectory(async (caseDir) => {
      const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), "observable-trace-symlink-output-"))
      const outside = await fs.mkdtemp(path.join(os.tmpdir(), "observable-trace-symlink-outside-"))
      try {
        await writeFinalizedCase(caseDir)
        await fs.symlink(outside, path.join(outputDir, "artifacts"))
        const before = await hashes(caseDir)

        const result = run("render", caseDir, "--output", path.join(outputDir, "trace.html"))
        const after = await hashes(caseDir)

        expect(result.exitCode).not.toBe(0)
        expect(await fs.readdir(outside)).toEqual([])
        expect(await fs.stat(path.join(outputDir, "trace.html")).catch(() => undefined)).toBeUndefined()
        expect(after).toEqual(before)
      } finally {
        await fs.rm(outputDir, { recursive: true, force: true })
        await fs.rm(outside, { recursive: true, force: true })
      }
    })
  })

  test("rolls back newly published snapshots when HTML publication fails", async () => {
    await withCaseDirectory(async (caseDir) => {
      await writeFinalizedCase(caseDir)
      await fs.mkdir(path.join(caseDir, "trace.html"))
      const before = await hashes(caseDir)

      const result = run("render", caseDir)
      const after = await hashes(caseDir)

      expect(result.exitCode).not.toBe(0)
      expect(after).toEqual(before)
      expect((await fs.stat(path.join(caseDir, "trace.html"))).isDirectory()).toBe(true)
      expect(
        await fs.stat(path.join(caseDir, "artifacts", "render-snapshots", "sha256")).catch(() => undefined),
      ).toBeUndefined()
    })
  })

  test("preserves pre-existing snapshots while rolling back newly published snapshots", async () => {
    await withCaseDirectory(async (caseDir) => {
      await writeFinalizedCase(caseDir)
      expect(run("render", caseDir).exitCode).toBe(0)
      const existingSnapshot = path.join(
        caseDir,
        "artifacts",
        "render-snapshots",
        "sha256",
        sha256(Buffer.from("authoritative artifact payload")),
      )
      await fs.rm(path.join(caseDir, "trace.html"))
      await fs.mkdir(path.join(caseDir, "trace.html"))
      await fs.writeFile(path.join(caseDir, "artifacts", "second.txt"), "new artifact payload")
      const traceFile = path.join(caseDir, "trace.json")
      const trace = JSON.parse(await fs.readFile(traceFile, "utf8")) as { artifacts: Record<string, unknown>[] }
      trace.artifacts.push({
        artifact_id: "second_artifact",
        kind: "text",
        label: "second",
        path: "artifacts/second.txt",
        length: 20,
        hash: "second-hash",
        preview: "new artifact payload",
        created_at: "2026-08-11T00:00:00.000Z",
        occurrences: 1,
        availability: "bundled",
      })
      await fs.writeFile(traceFile, JSON.stringify(trace))
      const before = await hashes(caseDir)

      const result = run("render", caseDir)
      const after = await hashes(caseDir)

      expect(result.exitCode).not.toBe(0)
      expect(await fs.readFile(existingSnapshot, "utf8")).toBe("authoritative artifact payload")
      expect(after).toEqual(before)
    })
  })

  test("executes the compiled package command entry against a finalized artifact case", async () => {
    const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), "observable-trace-compiled-"))
    try {
      const packageJSON = JSON.parse(await fs.readFile(path.resolve(import.meta.dir, "../package.json"), "utf8")) as {
        bin: Record<string, string>
      }
      const entry = path.resolve(import.meta.dir, "..", packageJSON.bin["observable-trace"])
      const executable = path.join(outputDir, "observable-trace")
      const built = runCommand(process.execPath, "build", "--compile", entry, "--outfile", executable)

      expect(entry).toBe(cli)
      expect(built.exitCode).toBe(0)
      await withCaseDirectory(async (caseDir) => {
        await writeFinalizedCase(caseDir)
        const before = await hashes(caseDir)
        const snapshot = path.join(
          caseDir,
          "artifacts",
          "render-snapshots",
          "sha256",
          sha256(Buffer.from("authoritative artifact payload")),
        )

        const result = runCommand(executable, "render", caseDir)
        const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
        const after = await hashes(caseDir)

        expect(result.exitCode).toBe(0)
        expect(Buffer.from(result.stdout).toString()).toContain("source: trace.json")
        expect(Buffer.from(result.stdout).toString()).toContain("completeness: complete")
        expect(Buffer.from(result.stdout).toString()).not.toContain("semantic payload must not be printed")
        expect(html).toContain(`href="artifacts/render-snapshots/sha256/${path.basename(snapshot)}"`)
        expect(await fs.readFile(snapshot, "utf8")).toBe("authoritative artifact payload")
        expect(before).toEqual(new Map([...after].filter(([file]) => before.has(file))))
      })
    } finally {
      await fs.rm(outputDir, { recursive: true, force: true })
    }
  })

  test("rejects malformed commands and unreadable inputs without producing output", async () => {
    await withCaseDirectory(async (caseDir) => {
      const invalidCommands = [
        run(),
        run("render"),
        run("inspect", caseDir),
        run("render", caseDir, "--output"),
        run("render", caseDir, "--output", path.join(caseDir, "trace.html"), "extra"),
        run("render", path.join(caseDir, "missing")),
      ]

      for (const result of invalidCommands) {
        expect(result.exitCode).not.toBe(0)
        expect(Buffer.from(result.stderr).toString()).toContain("observable-trace:")
      }
      expect(await fs.readdir(caseDir)).toEqual([])
    })
  })
})
