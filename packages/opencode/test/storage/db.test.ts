import { describe, expect, test } from "bun:test"
import path from "path"
import { Global } from "@opencode-ai/core/global"
import { InstallationChannel } from "@opencode-ai/core/installation/version"
import { Flag } from "@opencode-ai/core/flag/flag"
import { Database } from "@/storage/db"

describe("Database.Path", () => {
  test("returns database path for the current channel", () => {
    const expected = ["latest", "beta"].includes(InstallationChannel)
      ? path.join(Global.Path.data, "opencode.db")
      : path.join(Global.Path.data, `opencode-${InstallationChannel.replace(/[^a-zA-Z0-9._-]/g, "-")}.db`)
    expect(Database.getChannelPath()).toBe(expected)
  })

  test("can force the shared database path for a channel-isolated build", () => {
    const previous = Flag.OPENCODE_DISABLE_CHANNEL_DB
    Flag.OPENCODE_DISABLE_CHANNEL_DB = true

    try {
      expect(Database.getChannelPath()).toBe(path.join(Global.Path.data, "opencode.db"))
    } finally {
      Flag.OPENCODE_DISABLE_CHANNEL_DB = previous
    }
  })
})
