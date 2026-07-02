#!/usr/bin/env node
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { loadCases, reviewTraceSufficiency, summarizeReviews } from "./lib/stress-review.mjs"

const rootDir = path.dirname(fileURLToPath(import.meta.url))

function parseArgs(argv) {
  const args = { cases: path.join(rootDir, "cases.json"), traces: "", out: "" }
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === "--cases") args.cases = argv[++index]
    else if (arg === "--traces") args.traces = argv[++index]
    else if (arg === "--out") args.out = argv[++index]
    else if (arg === "--help" || arg === "-h") args.help = true
    else throw new Error(`unknown argument: ${arg}`)
  }
  return args
}

function usage() {
  return [
    "Usage: node analyze-trace-sufficiency.mjs --traces <trace-dir> --out <report-dir> [--cases cases.json]",
    "",
    "Example:",
    "  node packages/opencode/test/observability/stress-cases/analyze-trace-sufficiency.mjs \\",
    "    --traces /tmp/observable-opencode-stress-run/traces \\",
    "    --out /tmp/observable-opencode-stress-run/reports",
  ].join("\n")
}

function readCases(casesFile) {
  const casesRoot = path.dirname(path.resolve(casesFile))
  return loadCases(casesRoot)
}

export function analyzeTraceDirectory({ casesFile, tracesDir, outDir }) {
  const cases = readCases(casesFile)
  fs.mkdirSync(outDir, { recursive: true })
  const reviews = []
  for (const item of cases) {
    const traceFile = path.join(tracesDir, item.case_id, "trace.json")
    let review
    if (fs.existsSync(traceFile)) {
      const trace = JSON.parse(fs.readFileSync(traceFile, "utf8"))
      review = reviewTraceSufficiency({ caseDefinition: item, trace })
    } else {
      review = {
        case_id: item.case_id,
        title: item.title,
        category: item.category,
        ground_truth_root_cause: item.ground_truth_root_cause,
        trace_sufficiency: "insufficient",
        evidence_found: item.required_trace_evidence.map((required) => ({
          required,
          status: "missing",
          record_refs: [],
        })),
        can_offline_module_identify_root_cause: false,
        missing_semantics: item.required_trace_evidence,
        redundant_or_noisy_semantics: [],
        recommended_trace_changes: ["Trace file was not generated for this case."],
      }
    }
    reviews.push(review)
    fs.writeFileSync(path.join(outDir, `${item.case_id}.trace-review.json`), JSON.stringify(review, null, 2) + "\n")
  }
  fs.writeFileSync(path.join(outDir, "summary.md"), summarizeReviews(reviews))
  return reviews
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs(process.argv.slice(2))
  if (args.help) {
    console.log(usage())
    process.exit(0)
  }
  if (!args.traces || !args.out) {
    console.error(usage())
    process.exit(2)
  }
  const reviews = analyzeTraceDirectory({
    casesFile: path.resolve(args.cases),
    tracesDir: path.resolve(args.traces),
    outDir: path.resolve(args.out),
  })
  console.log(summarizeReviews(reviews))
}
