import { useRenderer } from "@opentui/solid"
import { createSimpleContext } from "./helper"
import { FormatError, FormatUnknownError } from "@/cli/error"
import { win32FlushInputBuffer } from "../win32"
type Exit = ((reason?: unknown) => Promise<void>) & {
  message: {
    set: (value?: string) => () => void
    clear: () => void
    get: () => string | undefined
  }
}

export const { use: useExit, provider: ExitProvider } = createSimpleContext({
  name: "Exit",
  init: (input: { onBeforeExit?: () => Promise<void>; onExit?: () => Promise<void> }) => {
    const renderer = useRenderer()
    let message: string | undefined
    let task: Promise<void> | undefined
    const store = {
      set: (value?: string) => {
        const prev = message
        message = value
        return () => {
          message = prev
        }
      },
      clear: () => {
        message = undefined
      },
      get: () => message,
    }
    const exit: Exit = Object.assign(
      (reason?: unknown) => {
        if (task) return task
        task = (async () => {
          disposeSignals()
          await input.onBeforeExit?.()
          // Reset window title before destroying renderer
          renderer.setTerminalTitle("")
          renderer.destroy()
          win32FlushInputBuffer()
          if (reason) {
            const formatted = FormatError(reason) ?? FormatUnknownError(reason)
            if (formatted) {
              process.stderr.write(formatted + "\n")
            }
          }
          const text = store.get()
          if (text) process.stdout.write(text + "\n")
          await input.onExit?.()
        })()
        return task
      },
      {
        message: store,
      },
    )
    const signals = [
      ["SIGHUP", 129],
      ["SIGINT", 130],
      ["SIGTERM", 143],
    ] as const
    const handlers = signals.map(([signal, exitCode]) => {
      const handler = () => {
        process.exitCode = exitCode
        void exit()
      }
      process.once(signal, handler)
      return [signal, handler] as const
    })
    const disposeSignals = () => {
      for (const [signal, handler] of handlers) process.off(signal, handler)
    }
    return exit
  },
})
