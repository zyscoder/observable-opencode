import { expect, test } from "bun:test"
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import {
  collectTracePublication,
  formatTracePublication,
  reportTracePublication,
} from "@/observability/trace-publication"

function createCaseDir() {
  return mkdtempSync(path.join(tmpdir(), "opencode-trace-publication-"))
}

test("reports a completed trace when its semantic JSON exists without HTML", () => {
  const caseDir = createCaseDir()
  const traceFile = path.join(caseDir, "trace.json")
  const partialFile = path.join(caseDir, "partial/latest.json")

  try {
    writeFileSync(traceFile, "{}")
    const publication = collectTracePublication({
      sessionID: "ses_test",
      caseID: "case_test",
      status: "completed",
      caseDir,
      traceFile,
      partialFile,
    })

    expect(publication?.status).toBe("completed")
    expect(publication?.caseDir).toBe(path.resolve(caseDir))
    expect(publication?.traceFile).toBe(path.resolve(traceFile))
    expect(publication?.partialFile).toBeUndefined()
    expect(formatTracePublication(publication!)).toContain("session: ses_test")
    expect(formatTracePublication(publication!)).toContain(path.resolve(traceFile))
    expect(formatTracePublication(publication!)).not.toContain("partial:")
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})

test("does not publish an HTML-only trace", () => {
  const caseDir = createCaseDir()
  const htmlFile = path.join(caseDir, "trace.html")

  try {
    writeFileSync(htmlFile, "<html></html>")
    expect(
      collectTracePublication({
        caseID: "case_html_only",
        status: "completed",
        caseDir,
        htmlFile,
      } as any),
    ).toBeUndefined()
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})

test("publishes a partial snapshot when complete trace files are incomplete", () => {
  const caseDir = createCaseDir()
  const partialFile = path.join(caseDir, "partial/latest.json")
  const relativeCaseDir = path.relative(process.cwd(), caseDir)
  const relativePartialFile = path.relative(process.cwd(), partialFile)

  try {
    mkdirSync(path.dirname(partialFile), { recursive: true })
    writeFileSync(partialFile, "{}")
    const publication = collectTracePublication({
      sessionID: "ses_partial",
      caseID: "case_partial",
      status: "completed",
      caseDir: relativeCaseDir,
      traceFile: path.join(relativeCaseDir, "trace.json"),
      partialFile: relativePartialFile,
    })

    expect(publication?.status).toBe("partial")
    expect(publication?.caseDir).toBe(path.resolve(caseDir))
    expect(publication?.traceFile).toBeUndefined()
    expect(publication?.partialFile).toBe(path.resolve(partialFile))
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})

test("returns no publication when no terminal file exists", () => {
  const caseDir = createCaseDir()

  try {
    expect(
      collectTracePublication({
        caseID: "case_missing",
        status: "failed",
        caseDir,
        traceFile: path.join(caseDir, "trace.json"),
        partialFile: path.join(caseDir, "partial/latest.json"),
      }),
    ).toBeUndefined()
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})

test("does not expose stderr writer failures", () => {
  const caseDir = createCaseDir()
  const traceFile = path.join(caseDir, "trace.json")

  try {
    writeFileSync(traceFile, "{}")
    const publication = collectTracePublication({
      caseID: "case_writer_failure",
      status: "failed",
      caseDir,
      traceFile,
    })

    expect(publication).toBeDefined()
    expect(() =>
      reportTracePublication(publication!, () => {
        throw new Error("writer failed")
      }),
    ).not.toThrow()
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})

test("does not expose asynchronous stderr writer rejections", async () => {
  const caseDir = createCaseDir()
  const traceFile = path.join(caseDir, "trace.json")

  try {
    writeFileSync(traceFile, "{}")
    const publication = collectTracePublication({
      caseID: "case_async_writer_failure",
      status: "failed",
      caseDir,
      traceFile,
    })

    expect(() =>
      reportTracePublication(publication!, () => Promise.reject(new Error("async writer failed"))),
    ).not.toThrow()
    await Bun.sleep(0)
  } finally {
    rmSync(caseDir, { recursive: true, force: true })
  }
})
