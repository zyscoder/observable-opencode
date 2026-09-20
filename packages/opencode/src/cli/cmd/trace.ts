import type { Argv } from "yargs"
import { cmd } from "./cmd"
import { finalize as finalizeTrace, render as renderTrace } from "@opencode-ai/trace-renderer/cli"

const FinalizeCommand = cmd({
  command: "finalize <caseDir>",
  describe: "materialize a segmented case trace into trace.json",
  builder: (yargs: Argv) =>
    yargs.positional("caseDir", {
      type: "string",
      demandOption: true,
      describe: "case trace directory",
    }),
  handler: (args) => {
    const result = finalizeTrace(["finalize", args.caseDir])
    console.log(`completeness: ${result.completeness}`)
    console.log(`trace: ${result.traceFile}`)
    console.log(`manifest: ${result.manifestFile}`)
    console.log(`partial: ${result.partialFile}`)
  },
})

const RenderCommand = cmd({
  command: "render <input> [output]",
  describe: "render trace.json or a segmented trace into trace.html",
  builder: (yargs: Argv) =>
    yargs
      .positional("input", {
        type: "string",
        demandOption: true,
        describe: "case trace directory or trace file",
      })
      .positional("output", {
        type: "string",
        describe: "optional HTML output path",
      }),
  handler: (args) => {
    const argv = ["render", args.input]
    if (args.output) argv.push("--output", args.output)
    const result = renderTrace(argv)
    console.log(`source: ${result.source}`)
    console.log(`completeness: ${result.incomplete ? "incomplete" : "complete"}`)
    console.log(`output: ${result.output}`)
  },
})

export const TraceCommand = cmd({
  command: "trace",
  describe: "offline trace materialization and rendering",
  builder: (yargs: Argv) => yargs.command(FinalizeCommand).command(RenderCommand).demandCommand(),
  handler: () => {},
})

