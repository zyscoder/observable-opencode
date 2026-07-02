import { EOL } from "os"
import path from "path"
import { drizzle } from "drizzle-orm/bun-sqlite"
import { Global } from "@opencode-ai/core/global"
import { Filesystem } from "@/util/filesystem"
import { JsonMigration } from "@/storage/json-migration"
import { Database } from "@/storage/db"

export function shouldRunStartupJsonMigration(input: { markerExists: boolean; legacyStorageExists: boolean }) {
  return !input.markerExists && input.legacyStorageExists
}

export async function runStartupJsonMigration() {
  const marker = path.join(Global.Path.data, "opencode.db")
  const legacyStorage = path.join(Global.Path.data, "storage")
  const markerExists = await Filesystem.exists(marker)
  const legacyStorageExists = await Filesystem.exists(legacyStorage)
  if (!shouldRunStartupJsonMigration({ markerExists, legacyStorageExists })) return

  const tty = process.stderr.isTTY
  process.stderr.write("Performing one time database migration, may take a few minutes..." + EOL)
  const width = 36
  const orange = "\x1b[38;5;214m"
  const muted = "\x1b[0;2m"
  const reset = "\x1b[0m"
  let last = -1
  if (tty) process.stderr.write("\x1b[?25l")
  try {
    await JsonMigration.run(drizzle({ client: Database.Client().$client }), {
      progress: (event) => {
        const percent = Math.floor((event.current / event.total) * 100)
        if (percent === last && event.current !== event.total) return
        last = percent
        if (tty) {
          const fill = Math.round((percent / 100) * width)
          const bar = `${"#".repeat(fill)}${".".repeat(width - fill)}`
          process.stderr.write(
            `\r${orange}${bar} ${percent.toString().padStart(3)}%${reset} ${muted}${event.label.padEnd(12)} ${event.current}/${event.total}${reset}`,
          )
          if (event.current === event.total) process.stderr.write("\n")
        } else {
          process.stderr.write(`sqlite-migration:${percent}${EOL}`)
        }
      },
    })
  } finally {
    if (tty) process.stderr.write("\x1b[?25h")
    else {
      process.stderr.write(`sqlite-migration:done${EOL}`)
    }
  }
  process.stderr.write("Database migration complete." + EOL)
}
