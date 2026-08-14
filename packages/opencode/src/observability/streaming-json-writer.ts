import fs from "node:fs"
import path from "node:path"

export type StreamingJsonRawItem = {
  type: "streaming_json_raw_item"
  json: string
}

export type StreamingJsonArray = {
  type: "streaming_json_array"
  items: Iterable<unknown | StreamingJsonRawItem>
}

export type StreamingJsonObjectMember = readonly [key: string, value: unknown | StreamingJsonArray]

export function streamingJsonRawItem(json: string): StreamingJsonRawItem {
  return { type: "streaming_json_raw_item", json }
}

export function streamingJsonArray(items: Iterable<unknown | StreamingJsonRawItem>): StreamingJsonArray {
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

export function writeStreamingJsonObjectAtomic(target: string, members: Iterable<StreamingJsonObjectMember>): void {
  const directory = path.dirname(target)
  const temporary = path.join(
    directory,
    `.${path.basename(target)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  )
  let fd: number | undefined

  const write = (text: string) => {
    let offset = 0
    while (offset < text.length) {
      const written = fs.writeSync(fd!, text.slice(offset), undefined, "utf8")
      if (written <= 0) throw new Error(`failed to write ${temporary}`)
      offset += written
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
        write(isRawItem(item) ? item.json : (JSON.stringify(item) ?? "null"))
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
