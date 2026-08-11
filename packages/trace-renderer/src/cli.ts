#!/usr/bin/env bun
import path from "node:path"
import { writeProvenanceTraceHtmlFile } from "./html"
import { loadRenderableTrace } from "./load"

const usage = "usage: observable-trace render <case-dir-or-file> [--output <path>]"

function parseRenderArguments(argv: string[]) {
  if (argv[0] !== "render") throw new Error(usage)
  if (argv.length === 2 && !argv[1].startsWith("-")) return { input: argv[1] }
  if (argv.length === 4 && !argv[1].startsWith("-") && argv[2] === "--output" && !argv[3].startsWith("-"))
    return { input: argv[1], output: argv[3] }
  throw new Error(usage)
}

export function render(argv: string[]) {
  const input = parseRenderArguments(argv)
  const loaded = loadRenderableTrace(path.resolve(input.input))
  const output = input.output ? path.resolve(input.output) : path.join(loaded.caseDir, "trace.html")
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
