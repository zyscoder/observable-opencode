import { materializeTrace } from "@/observability/trace-materializer"
import { cmd } from "./cmd"

export const TraceFinalizeCommand = cmd({
  command: "trace-finalize <caseDir>",
  describe: false,
  builder: (yargs) =>
    yargs.positional("caseDir", {
      describe: "case directory to materialize",
      type: "string",
      demandOption: true,
    }),
  handler(args) {
    const result = materializeTrace({ caseDir: args.caseDir })
    process.stdout.write(
      [
        `completeness: ${result.completeness}`,
        `trace: ${result.traceFile}`,
        `manifest: ${result.manifestFile}`,
        `partial: ${result.partialFile}`,
      ].join("\n") + "\n",
    )
  },
})
