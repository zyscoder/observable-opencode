// Serial prompt queue for direct interactive mode.
//
// Prompts arrive from the footer (user types and hits enter) and queue up
// here. The queue drains one turn at a time: it appends the user row to
// scrollback, calls input.run() to execute the turn through the stream
// transport, and waits for completion before starting the next prompt.
//
// The queue also handles /exit, /quit, and /new commands, empty-prompt rejection,
// and tracks per-turn wall-clock duration for the footer status line.
//
// Resolves when the footer closes and all in-flight work finishes.
import * as Locale from "@/util/locale"
import { isExitCommand, isNewCommand } from "./prompt.shared"
import type { FooterApi, FooterEvent, RunPrompt } from "./types"
import { CaseTrace } from "@/observability/case-trace"

type Trace = {
  write(type: string, data?: unknown): void
}

type Deferred<T = void> = {
  promise: Promise<T>
  resolve: (value: T | PromiseLike<T>) => void
  reject: (error?: unknown) => void
}

export type QueueInput = {
  footer: FooterApi
  initialInput?: string
  trace?: Trace
  sessionID?: () => string | undefined
  onSend?: (prompt: RunPrompt) => void
  onNewSession?: () => void | Promise<void>
  onTraceOutcome?: (input: { sessionID?: string; outcome: InteractiveTurnOutcome }) => void
  run: (prompt: RunPrompt, signal: AbortSignal) => Promise<void | InteractiveTurnOutcome>
}

export type InteractiveTurnOutcome =
  | { status: "success" }
  | { status: "error"; error: unknown }
  | { status: "cancelled" }

type QueuedPrompt = {
  prompt: RunPrompt
  sessionID?: string
  deferredSession: boolean
  enqueuedAt: number
}

type State = {
  queue: QueuedPrompt[]
  ctrl?: AbortController
  closed: boolean
  lastSessionID?: string
  pendingSessionSwitches: number
}

function defer<T = void>(): Deferred<T> {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (error?: unknown) => void
  const promise = new Promise<T>((next, fail) => {
    resolve = next
    reject = fail
  })

  return { promise, resolve, reject }
}

// Runs the prompt queue until the footer closes.
//
// Subscribes to footer prompt events, queues them, and drains one at a
// time through input.run(). If the user submits multiple prompts while
// a turn is running, they queue up and execute in order. The footer shows
// the queue depth so the user knows how many are pending.
export async function runPromptQueue(input: QueueInput): Promise<void> {
  const stop = defer<{ type: "closed" }>()
  const done = defer()
  const state: State = {
    queue: [],
    closed: input.footer.isClosed,
    pendingSessionSwitches: 0,
  }
  let draining: Promise<void> | undefined

  const emit = (next: FooterEvent, row: Record<string, unknown>) => {
    input.trace?.write("ui.patch", row)
    input.footer.event(next)
  }

  const finish = () => {
    if (!state.closed || draining) {
      return
    }

    done.resolve()
  }

  const close = () => {
    if (state.closed) {
      return
    }

    state.closed = true
    state.queue.length = 0
    state.ctrl?.abort()
    stop.resolve({ type: "closed" })
    finish()
  }

  const traceEnqueue = (queued: QueuedPrompt, sessionID: string | undefined) => {
    CaseTrace.event({
      component: "runtime",
      event_type: "queue.enqueue",
      data: {
        sessionID,
        prompt: CaseTrace.summarizeText(queued.prompt.text),
        part_count: queued.prompt.parts.length,
        queue: state.queue.length,
        new_session: isNewCommand(queued.prompt.text),
        deferred: queued.deferredSession,
        enqueued_at: queued.enqueuedAt,
      },
    })
  }

  const drain = () => {
    if (draining || state.closed || state.queue.length === 0) {
      return
    }

    draining = (async () => {
      try {
        while (!state.closed && state.queue.length > 0) {
          const queued = state.queue.shift()
          if (!queued) {
            continue
          }
          const { prompt } = queued
          const sessionID = queued.deferredSession ? input.sessionID?.() : queued.sessionID
          if (queued.deferredSession) {
            traceEnqueue(queued, sessionID)
          }
          state.lastSessionID = sessionID

          if (isNewCommand(prompt.text)) {
            emit(
              {
                type: "queue",
                queue: state.queue.length,
              },
              {
                queue: state.queue.length,
              },
            )
            if (!input.onNewSession) {
              state.pendingSessionSwitches = Math.max(0, state.pendingSessionSwitches - 1)
              emit(
                {
                  type: "stream.patch",
                  patch: {
                    status: "new sessions unavailable",
                  },
                },
                {
                  status: "new sessions unavailable",
                },
              )
              continue
            }

            emit(
              {
                type: "stream.patch",
                patch: {
                  phase: "running",
                  status: "starting new session",
                  queue: state.queue.length,
                },
              },
              {
                phase: "running",
                status: "starting new session",
                queue: state.queue.length,
              },
            )
            try {
              await input.onNewSession()
            } finally {
              state.pendingSessionSwitches = Math.max(0, state.pendingSessionSwitches - 1)
              state.lastSessionID = input.sessionID?.()
            }
            continue
          }

          emit(
            {
              type: "turn.send",
              queue: state.queue.length,
            },
            {
              phase: "running",
              status: "sending prompt",
              queue: state.queue.length,
            },
          )
          const start = Date.now()
          const ctrl = new AbortController()
          state.ctrl = ctrl
          const span = CaseTrace.startSpan({
            component: "runtime",
            operation: "turn",
            name: "interactive.turn",
            input: {
              sessionID,
              prompt: CaseTrace.summarizeText(prompt.text),
              queue: state.queue.length,
              part_count: prompt.parts.length,
            },
          })
          let spanEnded = false
          const endSpan = (next?: Parameters<NonNullable<typeof span>["end"]>[0]) => {
            if (spanEnded) return
            spanEnded = true
            span?.end(next)
          }
          const cancelSpan = (stage: string) => {
            const outcome = { status: "cancelled" as const }
            endSpan({
              status: "cancelled",
              output: {
                reason: "cancelled",
                stage,
                queue: state.queue.length,
              },
            })
            input.onTraceOutcome?.({ sessionID, outcome })
          }

          try {
            await input.footer.idle()
            if (state.closed) {
              cancelSpan("footer.idle")
              break
            }

            const commit = { kind: "user", text: prompt.text, phase: "start", source: "system" } as const
            input.trace?.write("ui.commit", commit)
            input.footer.append(commit)
            input.onSend?.(prompt)

            if (state.closed) {
              cancelSpan("footer.append")
              break
            }

            const task = input.run(prompt, ctrl.signal).then(
              (value) => ({ type: "done" as const, value }),
              (error) => ({ type: "error" as const, error }),
            )

            const next = await Promise.race([task, stop.promise])
            if (next.type === "closed") {
              ctrl.abort()
              cancelSpan("turn.run")
              break
            }

            if (next.type === "error") {
              throw next.error
            }
            const outcome = next.value
            if (outcome?.status === "error") {
              endSpan({ status: "error", error: outcome.error })
            } else if (outcome?.status === "cancelled") {
              endSpan({
                status: "cancelled",
                output: { reason: "cancelled", stage: "turn.run", queue: state.queue.length },
              })
            } else {
              endSpan({ output: { queue: state.queue.length } })
            }
            input.onTraceOutcome?.({ sessionID, outcome: outcome ?? { status: "success" } })
          } catch (error) {
            endSpan({
              status: "error",
              error,
            })
            input.onTraceOutcome?.({ sessionID, outcome: { status: "error", error } })
            throw error
          } finally {
            if (state.ctrl === ctrl) {
              state.ctrl = undefined
            }

            const duration = Locale.duration(Math.max(0, Date.now() - start))
            CaseTrace.event({
              component: "runtime",
              event_type: "turn.duration",
              data: {
                sessionID,
                duration,
                queue: state.queue.length,
              },
            })
            emit(
              {
                type: "turn.duration",
                duration,
              },
              {
                duration,
              },
            )
          }
        }
      } catch (error) {
        CaseTrace.event({
          component: "runtime",
          event_type: "queue.error",
          data: {
            sessionID: state.lastSessionID,
            error,
          },
        })
        done.reject(error)
        return
      } finally {
        draining = undefined
        emit(
          {
            type: "turn.idle",
            queue: state.queue.length,
          },
          {
            phase: "idle",
            status: "",
            queue: state.queue.length,
          },
        )
        CaseTrace.event({
          component: "runtime",
          event_type: "turn.idle",
          data: {
            sessionID: state.lastSessionID ?? input.sessionID?.(),
            queue: state.queue.length,
          },
        })
      }

      finish()
    })()
  }

  const submit = (prompt: RunPrompt) => {
    if (!prompt.text.trim() || state.closed) {
      return
    }

    if (isExitCommand(prompt.text)) {
      input.footer.close()
      return
    }

    const newSession = isNewCommand(prompt.text)
    const deferredSession = !newSession && state.pendingSessionSwitches > 0
    const queued: QueuedPrompt = {
      prompt,
      sessionID: deferredSession ? undefined : input.sessionID?.(),
      deferredSession,
      enqueuedAt: Date.now(),
    }
    if (newSession) {
      state.pendingSessionSwitches += 1
    }
    state.queue.push(queued)
    if (!deferredSession) {
      traceEnqueue(queued, queued.sessionID)
    }
    emit(
      {
        type: "queue",
        queue: state.queue.length,
      },
      {
        queue: state.queue.length,
      },
    )
    if (newSession) {
      drain()
      return
    }

    emit(
      {
        type: "first",
        first: false,
      },
      {
        first: false,
      },
    )
    drain()
  }

  const offPrompt = input.footer.onPrompt((prompt) => {
    submit(prompt)
  })
  const offClose = input.footer.onClose(() => {
    close()
  })

  try {
    if (state.closed) {
      return
    }

    submit({
      text: input.initialInput ?? "",
      parts: [],
    })
    finish()
    await done.promise
  } finally {
    offPrompt()
    offClose()
    close()
    await draining?.catch(() => {})
  }
}
