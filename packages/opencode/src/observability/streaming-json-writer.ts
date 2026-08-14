import fs from "node:fs"
import path from "node:path"

export type StreamingJsonRawItem = {
  type: "streaming_json_raw_item"
  json: string
}

export type StreamingJsonRawChunks = {
  type: "streaming_json_raw_chunks"
  chunks: Iterable<string>
}

export type StreamingJsonArray = {
  type: "streaming_json_array"
  items: Iterable<unknown | StreamingJsonRawItem | StreamingJsonRawChunks>
}

export type StreamingJsonObjectMember = readonly [key: string, value: unknown | StreamingJsonArray]

const MAX_WRITE_CHUNK_CHARACTERS = 16 * 1024

export function streamingJsonRawItem(json: string): StreamingJsonRawItem {
  return { type: "streaming_json_raw_item", json }
}

export function streamingJsonRawChunks(chunks: Iterable<string>): StreamingJsonRawChunks {
  return { type: "streaming_json_raw_chunks", chunks }
}

export function streamingJsonArray(
  items: Iterable<unknown | StreamingJsonRawItem | StreamingJsonRawChunks>,
): StreamingJsonArray {
  return { type: "streaming_json_array", items }
}

function isStreamingArray(value: unknown): value is StreamingJsonArray {
  return Boolean(
    value &&
      typeof value === "object" &&
      "type" in value &&
      (value as { type?: unknown }).type === "streaming_json_array",
  )
}

function isRawItem(value: unknown): value is StreamingJsonRawItem {
  return Boolean(
    value &&
      typeof value === "object" &&
      "type" in value &&
      (value as { type?: unknown }).type === "streaming_json_raw_item",
  )
}

function isRawChunks(value: unknown): value is StreamingJsonRawChunks {
  return Boolean(
    value &&
      typeof value === "object" &&
      "type" in value &&
      (value as { type?: unknown }).type === "streaming_json_raw_chunks",
  )
}

export function writeStreamingJsonObjectAtomic(target: string, members: Iterable<StreamingJsonObjectMember>): void {
  const directory = path.dirname(target)
  const temporary = path.join(
    directory,
    `.${path.basename(target)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  )
  let fd: number | undefined

  const write = (text: string) => {
    for (let sourceOffset = 0; sourceOffset < text.length; ) {
      let sourceEnd = Math.min(text.length, sourceOffset + MAX_WRITE_CHUNK_CHARACTERS)
      const last = text.charCodeAt(sourceEnd - 1)
      const next = text.charCodeAt(sourceEnd)
      if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) sourceEnd -= 1
      const buffer = Buffer.from(text.slice(sourceOffset, sourceEnd), "utf8")
      let byteOffset = 0
      while (byteOffset < buffer.byteLength) {
        const written = fs.writeSync(fd!, buffer, byteOffset, buffer.byteLength - byteOffset)
        if (written <= 0) throw new Error(`failed to write ${temporary}`)
        byteOffset += written
      }
      sourceOffset = sourceEnd
    }
  }

  try {
    fs.mkdirSync(directory, { recursive: true })
    fd = fs.openSync(temporary, "wx")
    write("{")
    let firstMember = true
    for (const [key, value] of members) {
      if (!isStreamingArray(value) && value === undefined) continue
      if (!firstMember) write(",")
      firstMember = false
      write(`${JSON.stringify(key)}:`)
      if (!isStreamingArray(value)) {
        write(JSON.stringify(value) ?? "null")
        continue
      }
      write("[")
      let firstItem = true
      for (const item of value.items) {
        if (!firstItem) write(",")
        firstItem = false
        if (isRawChunks(item)) {
          for (const chunk of item.chunks) write(chunk)
        } else {
          write(isRawItem(item) ? item.json : (JSON.stringify(item) ?? "null"))
        }
      }
      write("]")
    }
    write("}")
    fs.fsyncSync(fd)
    fs.closeSync(fd)
    fd = undefined
    fs.renameSync(temporary, target)
  } catch (error) {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd)
      } catch {}
    }
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}
