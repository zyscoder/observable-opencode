import fs from "node:fs"
import path from "node:path"
import { finalizeTuiWorker } from "../../../../src/cli/cmd/tui/thread"
import { materializeWorkerTraces } from "../../../../src/cli/cmd/tui/trace-materializer-process"
import { reportTracePublication } from "../../../../src/observability/trace-publication"

const mode = process.argv[2]
process.env.TASK7_TUI_MODE = mode
const workerEnv = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
)
const worker = new Worker(new URL("./task-7-tui-worker.ts", import.meta.url), { env: workerEnv })
const pending = new Map<number, (value: any) => void>()
let nextID = 0
let markReady: () => void
const ready = new Promise<void>((resolve) => {
  markReady = resolve
})

worker.onmessage = (event) => {
  if (event.data?.ready) {
    markReady()
    return
  }
  const resolve = pending.get(event.data?.id)
  if (!resolve) return
  pending.delete(event.data.id)
  resolve(event.data.value)
}

function call<T>(method: "shutdown" | "closeTraces") {
  const id = ++nextID
  return new Promise<T>((resolve) => {
    pending.set(id, resolve)
    worker.postMessage({ id, method })
  })
}

let stopping: Promise<void> | undefined
function stop() {
  if (stopping) return stopping
  stopping = finalizeTuiWorker({
    shutdown: () => call("shutdown"),
    closeTraces: () => call("closeTraces"),
    terminate: () => worker.terminate(),
    materialize: (requests) =>
      materializeWorkerTraces(requests, {
        command: [process.execPath, path.resolve(import.meta.dir, "../../../../src/index.ts")],
        env: process.env,
        onWarning: (warning) => process.stderr.write(warning),
      }),
    publish: reportTracePublication,
    env: process.env,
  })
  return stopping
}

await ready

if (mode === "normal") {
  fs.writeFileSync(process.env.TASK7_TUI_READY_FILE!, "ready")
  await stop()
  process.exit(0)
}

const signal = mode === "sigint" ? "SIGINT" : mode === "sigterm" ? "SIGTERM" : undefined
if (!signal) throw new Error(`unknown Task 7 TUI fixture mode: ${mode}`)
process.once(signal, () => {
  void stop().then(() => process.exit(signal === "SIGINT" ? 130 : 143))
})
fs.writeFileSync(process.env.TASK7_TUI_READY_FILE!, "ready")
await new Promise<never>(() => {})
