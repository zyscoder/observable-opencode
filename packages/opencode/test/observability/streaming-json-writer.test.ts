import { expect, spyOn, test } from "bun:test"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  streamingJsonArray,
  streamingJsonRawItem,
  writeStreamingJsonObjectAtomic,
} from "@/observability/streaming-json-writer"

test("writes exact multibyte JSON bytes through bounded simulated short writes", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-streaming-json-writer-"))
  const target = path.join(directory, "trace.json")
  const rawPrefix = '{"payload":"'
  const payload = ["x".repeat(16 * 1024 - rawPrefix.length - 1), "🙂汉字", '\\"\n'].join("")
  const raw = JSON.stringify({ payload })
  const expected = `{"title":"多字节🙂","items":[${raw}]}`
  const originalWriteSync = fs.writeSync.bind(fs)
  let stringWrites = 0
  let maxBufferBytes = 0
  const writeSync = spyOn(fs, "writeSync").mockImplementation(((
    fd: number,
    data: string | NodeJS.ArrayBufferView,
    offset?: number,
    length?: number | string,
  ) => {
    if (typeof data === "string") {
      stringWrites += 1
      const encoded = Buffer.from(data, typeof length === "string" ? (length as BufferEncoding) : "utf8")
      const bytes = Math.min(encoded.byteLength, 7)
      return originalWriteSync(fd, encoded, 0, bytes)
    }
    const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength)
    const start = offset ?? 0
    const requested = typeof length === "number" ? length : buffer.byteLength - start
    const bytes = Math.min(requested, 7)
    maxBufferBytes = Math.max(maxBufferBytes, requested)
    return originalWriteSync(fd, buffer, start, bytes)
  }) as typeof fs.writeSync)

  try {
    writeStreamingJsonObjectAtomic(target, [
      ["title", "多字节🙂"],
      ["items", streamingJsonArray([streamingJsonRawItem(raw)])],
    ])

    const actual = fs.readFileSync(target)
    expect(actual).toEqual(Buffer.from(expected))
    expect(JSON.parse(actual.toString("utf8"))).toEqual(JSON.parse(expected))
    expect(stringWrites).toBe(0)
    expect(maxBufferBytes).toBeLessThanOrEqual(64 * 1024)
  } finally {
    writeSync.mockRestore()
    fs.rmSync(directory, { recursive: true, force: true })
  }
})
