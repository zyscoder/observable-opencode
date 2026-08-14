import { cmd } from "@/cli/cmd/cmd"
import { Rpc } from "@/util/rpc"
import { type rpc } from "./worker"
import path from "path"
import { fileURLToPath } from "url"
import { UI } from "@/cli/ui"
import * as Log from "@opencode-ai/core/util/log"
import { errorMessage } from "@/util/error"
import { withTimeout } from "@/util/timeout"
import { withNetworkOptions, resolveNetworkOptionsNoConfig } from "@/cli/network"
import { Filesystem } from "@/util/filesystem"
import type { GlobalEvent } from "@opencode-ai/sdk/v2"
import type { EventSource } from "./context/sdk"
import { win32DisableProcessedInput, win32InstallCtrlCGuard } from "./win32"
import { writeHeapSnapshot } from "v8"
import { TuiConfig } from "./config/tui"
import {
  OPENCODE_PROCESS_ROLE,
  OPENCODE_RUN_ID,
  ensureRunID,
  sanitizedProcessEnv,
} from "@opencode-ai/core/util/opencode-process"
import { validateSession } from "./validate-session"
import { materializeWorkerTraces } from "./trace-materializer-process"
import { reportTracePublication } from "@/observability/trace-publication"
import type { WorkerTraceCloseResult } from "./worker-trace"

declare global {
  const OPENCODE_WORKER_PATH: string
}

type RpcClient = ReturnType<typeof Rpc.client<typeof rpc>>

const DEFAULT_TUI_WORKER_SHUTDOWN_TIMEOUT_MS = 5_000
const TRACED_TUI_WORKER_SHUTDOWN_TIMEOUT_MS = 60 * 60 * 1_000

function traceEnabled(env: NodeJS.ProcessEnv | Record<string, string | undefined>) {
  const value = env.OPENCODE_CASE_TRACE?.trim().toLowerCase()
  return value === "1" || value === "true" || value === "yes" || value === "on"
}

export function resolveTuiWorkerShutdownTimeout(
  env: NodeJS.ProcessEnv | Record<string, string | undefined> = process.env,
) {
  return DEFAULT_TUI_WORKER_SHUTDOWN_TIMEOUT_MS
}

export function resolveTuiWorkerTraceCloseTimeout(
  env: NodeJS.ProcessEnv | Record<string, string | undefined> = process.env,
) {
  const configured = Number(env.OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS)
  if (Number.isSafeInteger(configured) && configured > 0) return configured
  return traceEnabled(env) ? TRACED_TUI_WORKER_SHUTDOWN_TIMEOUT_MS : DEFAULT_TUI_WORKER_SHUTDOWN_TIMEOUT_MS
}

export function waitForTuiWorkerShutdown<T>(
  shutdown: Promise<T>,
  env: NodeJS.ProcessEnv | Record<string, string | undefined> = process.env,
) {
  const timeout = resolveTuiWorkerShutdownTimeout(env)
  return withTimeout(shutdown, timeout, `TUI worker shutdown timed out after ${timeout}ms`)
}

export function waitForTuiWorkerTraceClose<T>(
  close: Promise<T>,
  env: NodeJS.ProcessEnv | Record<string, string | undefined> = process.env,
) {
  const timeout = resolveTuiWorkerTraceCloseTimeout(env)
  return withTimeout(close, timeout, `TUI worker trace close timed out after ${timeout}ms`)
}

export async function finalizeTuiWorker(input: {
  shutdown: () => Promise<{ failure?: string }>
  closeTraces: () => Promise<WorkerTraceCloseResult>
  terminate: () => unknown
  materialize?: typeof materializeWorkerTraces
  publish?: typeof reportTracePublication
  onShutdownFailure?: (error: unknown) => unknown
  onTerminateFailure?: (error: unknown) => unknown
  onMaterializerFailure?: (error: unknown) => unknown
  env?: NodeJS.ProcessEnv | Record<string, string | undefined>
}): Promise<void> {
  const shutdown = await waitForTuiWorkerShutdown(input.shutdown(), input.env).catch((error) => {
    try {
      input.onShutdownFailure?.(error)
    } catch {}
    return undefined
  })
  const trace: WorkerTraceCloseResult = shutdown
    ? await waitForTuiWorkerTraceClose(input.closeTraces(), input.env).catch((error) => {
        try {
          input.onShutdownFailure?.(error)
        } catch {}
        return { requests: [] }
      })
    : { requests: [] }
  if (trace.failure) {
    try {
      input.onShutdownFailure?.(new Error(trace.failure))
    } catch {}
  }
  await Promise.resolve().then(input.terminate).catch((error) => {
    try {
      input.onTerminateFailure?.(error)
    } catch {}
  })
  try {
    const publications = await (input.materialize ?? materializeWorkerTraces)(trace.requests)
    const publish = input.publish ?? reportTracePublication
    publications.forEach((publication) => {
      try {
        publish(publication)
      } catch {}
    })
  } catch (error) {
    try {
      input.onMaterializerFailure?.(error)
    } catch {}
  }
}

function createWorkerFetch(client: RpcClient): typeof fetch {
  const fn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(input, init)
    const body = request.body ? await request.text() : undefined
    const result = await client.call("fetch", {
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(request.headers.entries()),
      body,
    })
    return new Response(result.body, {
      status: result.status,
      headers: result.headers,
    })
  }
  return fn as typeof fetch
}

function createEventSource(client: RpcClient): EventSource {
  return {
    subscribe: async (handler) => {
      return client.on<GlobalEvent>("global.event", (e) => {
        handler(e)
      })
    },
  }
}

async function target() {
  if (typeof OPENCODE_WORKER_PATH !== "undefined") return OPENCODE_WORKER_PATH
  const dist = new URL("./cli/cmd/tui/worker.js", import.meta.url)
  if (await Filesystem.exists(fileURLToPath(dist))) return dist
  return new URL("./worker.ts", import.meta.url)
}

async function input(value?: string) {
  const piped = process.stdin.isTTY ? undefined : await Bun.stdin.text()
  if (!value) return piped
  if (!piped) return value
  return piped + "\n" + value
}

export function resolveThreadDirectory(project?: string, envPWD = process.env.PWD, cwd = process.cwd()) {
  const root = Filesystem.resolve(envPWD ?? cwd)
  if (project) return Filesystem.resolve(path.isAbsolute(project) ? project : path.join(root, project))
  return Filesystem.resolve(cwd)
}

export const TuiThreadCommand = cmd({
  command: "$0 [project]",
  describe: "start opencode tui",
  builder: (yargs) =>
    withNetworkOptions(yargs)
      .positional("project", {
        type: "string",
        describe: "path to start opencode in",
      })
      .option("model", {
        type: "string",
        alias: ["m"],
        describe: "model to use in the format of provider/model",
      })
      .option("continue", {
        alias: ["c"],
        describe: "continue the last session",
        type: "boolean",
      })
      .option("session", {
        alias: ["s"],
        type: "string",
        describe: "session id to continue",
      })
      .option("fork", {
        type: "boolean",
        describe: "fork the session when continuing (use with --continue or --session)",
      })
      .option("prompt", {
        type: "string",
        describe: "prompt to use",
      })
      .option("agent", {
        type: "string",
        describe: "agent to use",
      }),
  handler: async (args) => {
    // Keep ENABLE_PROCESSED_INPUT cleared even if other code flips it.
    // (Important when running under `bun run` wrappers on Windows.)
    const unguard = win32InstallCtrlCGuard()
    try {
      // Must be the very first thing — disables CTRL_C_EVENT before any Worker
      // spawn or async work so the OS cannot kill the process group.
      win32DisableProcessedInput()

      if (args.fork && !args.continue && !args.session) {
        UI.error("--fork requires --continue or --session")
        process.exitCode = 1
        return
      }

      // Resolve relative --project paths from PWD, then use the real cwd after
      // chdir so the thread and worker share the same directory key.
      const next = resolveThreadDirectory(args.project)
      const file = await target()
      try {
        process.chdir(next)
      } catch {
        UI.error("Failed to change directory to " + next)
        return
      }
      const cwd = Filesystem.resolve(process.cwd())
      const env = sanitizedProcessEnv({
        [OPENCODE_PROCESS_ROLE]: "worker",
        [OPENCODE_RUN_ID]: ensureRunID(),
      })

      const worker = new Worker(file, {
        env,
      })
      worker.onerror = (e) => {
        Log.Default.error("thread error", {
          message: e.message,
          filename: e.filename,
          lineno: e.lineno,
          colno: e.colno,
          error: e.error,
        })
      }

      const client = Rpc.client<typeof rpc>(worker)
      const error = (e: unknown) => {
        Log.Default.error("process error", { error: errorMessage(e) })
      }
      const reload = () => {
        client.call("reload", undefined).catch((err) => {
          Log.Default.warn("worker reload failed", {
            error: errorMessage(err),
          })
        })
      }
      process.on("uncaughtException", error)
      process.on("unhandledRejection", error)
      process.on("SIGUSR2", reload)

      let stopped = false
      const stop = async () => {
        if (stopped) return
        stopped = true
        process.off("uncaughtException", error)
        process.off("unhandledRejection", error)
        process.off("SIGUSR2", reload)
        await finalizeTuiWorker({
          shutdown: async () => await client.call("shutdown", undefined),
          closeTraces: async () => await client.call("closeTraces", undefined),
          terminate: () => worker.terminate(),
          materialize: (requests) =>
            materializeWorkerTraces(requests, {
              onWarning: (warning) => process.stderr.write(warning),
            }),
          env: process.env,
          onShutdownFailure(error) {
            Log.Default.warn("worker shutdown failed", {
              error: errorMessage(error),
            })
            if (traceEnabled(process.env)) {
              process.stderr.write(
                `[observable-opencode] Worker shutdown or trace close did not complete; trace.json may be unavailable: ${errorMessage(error)}\n`,
              )
            }
          },
          onMaterializerFailure(error) {
            Log.Default.warn("trace materialization failed", {
              error: errorMessage(error),
            })
          },
          onTerminateFailure(error) {
            Log.Default.warn("worker termination failed", {
              error: errorMessage(error),
            })
          },
        })
      }

      const prompt = await input(args.prompt)
      const config = await TuiConfig.get()

      const network = resolveNetworkOptionsNoConfig(args)
      const external =
        process.argv.includes("--port") ||
        process.argv.includes("--hostname") ||
        process.argv.includes("--mdns") ||
        network.mdns ||
        network.port !== 0 ||
        network.hostname !== "127.0.0.1"

      const transport = external
        ? {
            url: (await client.call("server", network)).url,
            fetch: undefined,
            events: undefined,
          }
        : {
            url: "http://opencode.internal",
            fetch: createWorkerFetch(client),
            events: createEventSource(client),
          }

      try {
        await validateSession({
          url: transport.url,
          sessionID: args.session,
          directory: cwd,
          fetch: transport.fetch,
        })
      } catch (error) {
        UI.error(errorMessage(error))
        process.exitCode = 1
        return
      }

      setTimeout(() => {
        client.call("checkUpgrade", { directory: cwd }).catch(() => {})
      }, 1000).unref?.()

      try {
        const { tui } = await import("./app")
        await tui({
          url: transport.url,
          async onSnapshot() {
            const tui = writeHeapSnapshot("tui.heapsnapshot")
            const server = await client.call("snapshot", undefined)
            return [tui, server]
          },
          config,
          directory: cwd,
          fetch: transport.fetch,
          events: transport.events,
          args: {
            continue: args.continue,
            sessionID: args.session,
            agent: args.agent,
            model: args.model,
            prompt,
            fork: args.fork,
          },
        })
      } finally {
        await stop()
      }
    } finally {
      unguard?.()
    }
    process.exit(0)
  },
})
// scratch
