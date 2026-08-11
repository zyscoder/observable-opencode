#!/usr/bin/env bun
import fs from "node:fs"
import path from "node:path"
import { writeProvenanceTraceHtmlFile } from "./html"
import { loadRenderableTrace } from "./load"
import { safeArtifactRelativePath } from "./viewer"

const usage = "usage: observable-trace render <case-dir-or-file> [--output <path>]"

function parseRenderArguments(argv: string[]) {
  if (argv[0] !== "render") throw new Error(usage)
  if (argv.length === 2 && !argv[1].startsWith("-")) return { input: argv[1] }
  if (argv.length === 4 && !argv[1].startsWith("-") && argv[2] === "--output" && !argv[3].startsWith("-"))
    return { input: argv[1], output: argv[3] }
  throw new Error(usage)
}

const semanticTraceFiles = [
  "events.jsonl",
  "raw-events.jsonl",
  "records.jsonl",
  "trace.json",
  "manifest.json",
  "legacy-trace.json",
  "provenance-trace.json",
  "partial/latest.json",
]

function existingPathIdentity(input: string) {
  try {
    const stats = fs.statSync(input)
    return { realPath: fs.realpathSync(input), dev: stats.dev, ino: stats.ino }
  } catch {
    return undefined
  }
}

function outputAliasesProtectedPath(output: string, protectedPath: string) {
  if (path.resolve(output) === path.resolve(protectedPath)) return true
  const outputIdentity = existingPathIdentity(output)
  const protectedIdentity = existingPathIdentity(protectedPath)
  return Boolean(
    outputIdentity &&
      protectedIdentity &&
      (outputIdentity.realPath === protectedIdentity.realPath ||
        (outputIdentity.dev === protectedIdentity.dev && outputIdentity.ino === protectedIdentity.ino)),
  )
}

function assertOutputDoesNotCollide(output: string, loaded: ReturnType<typeof loadRenderableTrace>) {
  const protectedPaths = new Set(semanticTraceFiles.map((relative) => path.resolve(loaded.caseDir, relative)))
  const files = (loaded.trace.manifest as Record<string, unknown>).files
  if (files && typeof files === "object" && !Array.isArray(files)) {
    for (const value of Object.values(files)) {
      if (typeof value !== "string" || !value || path.isAbsolute(value)) continue
      const resolved = path.resolve(loaded.caseDir, value)
      const relative = path.relative(loaded.caseDir, resolved)
      if (relative && !relative.startsWith("..") && !path.isAbsolute(relative)) protectedPaths.add(resolved)
    }
  }
  for (const artifact of loaded.trace.artifacts) {
    if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) continue
    const relative = safeArtifactRelativePath((artifact as { path?: unknown }).path)
    if (relative) protectedPaths.add(path.resolve(loaded.caseDir, relative))
  }
  for (const protectedPath of protectedPaths) {
    if (outputAliasesProtectedPath(output, protectedPath)) {
      throw new Error(`${output}: output collides with trace semantic input ${protectedPath}`)
    }
  }
}

export function render(argv: string[]) {
  const input = parseRenderArguments(argv)
  const loaded = loadRenderableTrace(path.resolve(input.input))
  const output = input.output ? path.resolve(input.output) : path.join(loaded.caseDir, "trace.html")
  assertOutputDoesNotCollide(output, loaded)
  writeProvenanceTraceHtmlFile(output, loaded.trace as Parameters<typeof writeProvenanceTraceHtmlFile>[1], {
    artifactSourceRoot: loaded.caseDir,
  })
  return { source: loaded.source, incomplete: loaded.incomplete, output }
}

if (import.meta.main) {
  try {
    const result = render(process.argv.slice(2))
    console.log(`source: ${result.source}`)
    console.log(`completeness: ${result.incomplete ? "incomplete" : "complete"}`)
    console.log(`output: ${result.output}`)
  } catch (error) {
    console.error(`observable-trace: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  }
}
