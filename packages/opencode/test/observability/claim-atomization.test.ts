import { describe, expect, test } from "bun:test"
import * as claimAtomization from "../../src/observability/claim-atomization"
import { atomizeResponseClaims } from "../../src/observability/claim-atomization"
import { isBrokenClaimFragment, isNonFactualResponseClaim } from "../../src/observability/claim-atomization-core"
import { atomizeResponseClaimsWithLexerForTest } from "../../src/observability/claim-atomization.test-support"

describe("claim atomization", () => {
  test("keeps internal claim filters total for hostile coercion inputs", () => {
    const hostileInputs: unknown[] = [
      new Proxy(
        {},
        {
          get() {
            throw new Error("proxy access failed")
          },
        },
      ),
      {
        toJSON: () => undefined,
        [Symbol.toPrimitive]: () => {
          throw new Error("primitive conversion failed")
        },
      },
      {
        toJSON: () => undefined,
        toString: () => {
          throw new Error("string conversion failed")
        },
      },
    ]

    for (const input of hostileInputs) {
      expect(() => isNonFactualResponseClaim(input)).not.toThrow()
      expect(isNonFactualResponseClaim(input)).toBe(false)
      expect(() => isBrokenClaimFragment(input)).not.toThrow()
      expect(isBrokenClaimFragment(input)).toBe(false)
    }
  })

  test("keeps the production module export surface limited to the atomizer", () => {
    expect(Object.keys(claimAtomization).sort()).toEqual(["atomizeResponseClaims"])
  })

  test("keeps a parenthetical cstack statement as one auditable claim", () => {
    const response =
      "When `_cstack` handled a non-Model `right` operand (i.e., a pre-computed separability matrix), it now returns the matrix directly."
    const claims = atomizeResponseClaims(response)

    expect(claims).toHaveLength(1)
    expect(claims[0]).toMatchObject({
      text: response,
      raw_text: response,
      claim_index: 1,
      claim_count: 1,
      atomization_status: "atomic",
    })
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength(response)])
    expect(claims[0]!.text).not.toMatch(/^[,，;；)）\]］}｝]/)
  })

  test("preserves parenthetical bracket state across line boundaries", () => {
    const response = "The result (see\nthe detailed source). All 11 tests pass."

    expect(atomizeResponseClaims(response).map((claim) => claim.text)).toEqual([
      "The result (see the detailed source).",
      "All 11 tests pass.",
    ])
  })

  test("treats markdown headings as barriers for an open parenthetical claim", () => {
    const response = "Before 11 (see\n## Summary\nAfter 12 tests pass."

    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["After 12 tests pass."])
    expect(claims[0]!.source_byte_range).toEqual([
      Buffer.byteLength("Before 11 (see\n## Summary\n"),
      Buffer.byteLength(response),
    ])
  })

  test("treats fenced code as a barrier for an open parenthetical claim", () => {
    const response = "Before 11 (see\n```ts\nconst ignored = true\n```\nAfter 12 tests pass."

    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["After 12 tests pass."])
    expect(claims[0]!.source_byte_range).toEqual([
      Buffer.byteLength("Before 11 (see\n```ts\nconst ignored = true\n```\n"),
      Buffer.byteLength(response),
    ])
  })

  test("uses UTF-8 byte offsets and preserves claim order", () => {
    const response = "修改完成。All 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((item) => item.text)).toEqual(["修改完成。", "All 11 tests pass."])
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength("修改完成。")])
    expect(claims[1]!.source_byte_range[0]).toBe(Buffer.byteLength("修改完成。"))
    expect(claims.every((item) => item.claim_group_id)).toBe(true)
  })

  test("maps marked paragraph LF tokens back to CRLF source ranges", () => {
    const response = "修改完成。\r\nAll 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["修改完成。", "All 11 tests pass."])
    expect(claims.map((claim) => claim.raw_text)).toEqual(["修改完成。", "All 11 tests pass."])
    expect(claims[1]!.source_byte_range).toEqual([Buffer.byteLength("修改完成。\r\n"), Buffer.byteLength(response)])
  })

  test("maps marked list LF tokens back to CRLF source ranges", () => {
    const response = "- 所有 11 个测试通过。\r\n- All 12 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["所有 11 个测试通过。", "All 12 tests pass."])
    expect(claims.map((claim) => claim.raw_text)).toEqual(["所有 11 个测试通过。", "All 12 tests pass."])
    expect(claims[1]!.source_byte_range).toEqual([
      Buffer.byteLength("- 所有 11 个测试通过。\r\n- "),
      Buffer.byteLength(response),
    ])
  })

  test("maps marked table LF tokens back to CRLF source ranges", () => {
    const response = ["| 项目 | 值 |", "| --- | --- |", "| 测试😀 | All 11 tests pass. |"].join("\r\n")
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["测试😀: All 11 tests pass."])
    expect(claims[0]).toMatchObject({
      raw_text: "| 测试😀 | All 11 tests pass. |",
      table_cells: ["测试😀", "All 11 tests pass."],
      table_subject: "测试😀",
      table_values: ["All 11 tests pass."],
    })
    expect(claims[0]!.source_byte_range).toEqual([
      Buffer.byteLength("| 项目 | 值 |\r\n| --- | --- |\r\n"),
      Buffer.byteLength(response),
    ])
  })

  test("keeps CRLF mapping monotonic at the scan limit", () => {
    const prefix = "All 11 tests pass.\r\n`"
    const response = `${prefix}${"a".repeat(7_998 - prefix.length)}\r\nAfter 12 tests pass.`
    const claims = atomizeResponseClaims(response)

    expect(claims[0]!.text).toBe("All 11 tests pass.")
    expect(claims.every((claim) => !claim.text.includes("After 12 tests pass."))).toBe(true)
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength("All 11 tests pass.")])
    expect(claims[0]!.raw_text).toBe("All 11 tests pass.")
  })

  test("drops a continuation fragment without a preceding claim", () => {
    expect(atomizeResponseClaims(", the discount cap remains 15%.")).toEqual([])
  })

  test("does not let protected inline code change outer bracket state", () => {
    const response = "The implementation uses `foo(`. All 11 tests pass."

    expect(atomizeResponseClaims(response).map((claim) => claim.text)).toEqual([
      "The implementation uses `foo(`.",
      "All 11 tests pass.",
    ])
  })

  test("uses original-response byte ranges after markdown scaffold removal", () => {
    const response = "## Summary\n```ts\nconst ignored = true\n```\n修改完成。All 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["修改完成。", "All 11 tests pass."])
    const claimStart = Buffer.byteLength("## Summary\n```ts\nconst ignored = true\n```\n")
    expect(claims[0]!.source_byte_range).toEqual([claimStart, claimStart + Buffer.byteLength("修改完成。")])
    expect(claims[1]!.source_byte_range).toEqual([
      claimStart + Buffer.byteLength("修改完成。"),
      Buffer.byteLength(response),
    ])
  })

  test("merges a valid continuation into the preceding claim", () => {
    const claims = atomizeResponseClaims("Discount cap is 15%;, it remains enforced. All 11 tests pass.")

    expect(claims.map((claim) => claim.text)).toEqual([
      "Discount cap is 15%;, it remains enforced.",
      "All 11 tests pass.",
    ])
  })

  test("keeps groups and ordering links stable for identical input", () => {
    const response = "Owner is billing-platform. All 11 tests pass."
    const first = atomizeResponseClaims(response)
    const second = atomizeResponseClaims(response)

    expect(second).toEqual(first)
    expect(first[0]).toMatchObject({
      claim_index: 1,
      claim_count: 2,
      previous_claim_key: undefined,
      next_claim_key: first[1]!.key,
    })
    expect(first[1]).toMatchObject({
      claim_index: 2,
      claim_count: 2,
      previous_claim_key: first[0]!.key,
      next_claim_key: undefined,
    })
    expect(first.map((claim) => claim.claim_group_id)).toEqual(second.map((claim) => claim.claim_group_id))
  })

  test("drops an open span before structural markdown barriers", () => {
    const cases = [
      {
        name: "empty paragraph",
        response: "Before 11 (open\n\nAfter 12 tests pass.",
        expected: ["After 12 tests pass."],
      },
      {
        name: "table",
        response:
          "Before 11 (open\n| Result | Value |\n| --- | --- |\n| Tests | All 11 tests pass. |\nAfter 12 tests pass.",
        expected: ["Tests: All 11 tests pass.", "After 12 tests pass."],
      },
      {
        name: "blockquote",
        response: "Before 11 (open\n> Quoted 11 tests pass.\n\nAfter 12 tests pass.",
        expected: ["After 12 tests pass."],
      },
    ]

    for (const item of cases) {
      expect(
        atomizeResponseClaims(item.response).map((claim) => claim.text),
        item.name,
      ).toEqual(item.expected)
    }
  })

  test("treats CommonMark and GFM blocks as hard barriers", () => {
    const cases = [
      { name: "ATX heading", block: "## Summary" },
      { name: "Setext equals heading", block: "Summary\n=======" },
      { name: "Setext dash heading", block: "Summary\n-------" },
      { name: "thematic break", block: "***" },
      { name: "backtick fence", block: "```ts\nconst ignored = true\n```" },
      { name: "tilde fence", block: "~~~ts\nconst ignored = true\n~~~" },
      { name: "indented code", block: "\n    const ignored = true" },
      { name: "HTML block", block: "<div>\nIgnored 11 tests pass.\n</div>" },
      { name: "link definition", block: "[result]: https://example.com/tests" },
      { name: "blockquote", block: "> Quoted 11 tests pass." },
    ]

    for (const item of cases) {
      const response = `Before 11 (open\n${item.block}\n\nAfter 12 tests pass.`
      expect(
        atomizeResponseClaims(response).map((claim) => claim.text),
        item.name,
      ).toEqual(["After 12 tests pass."])
    }
  })

  test("preserves facts inside ordered, unordered, and nested lists", () => {
    const cases = [
      { name: "unordered", block: "- Owner is billing-platform." },
      { name: "ordered", block: "1. All 11 tests pass." },
      {
        name: "nested",
        block: "- Owner is billing-platform.\n  - All 11 tests pass.\n- The discount cap remains 15%.",
      },
    ]

    for (const item of cases) {
      const response = `Before 11 (open\n${item.block}\nAfter 12 tests pass.`
      const texts = atomizeResponseClaims(response).map((claim) => claim.text)
      expect(texts.at(-1), item.name).toBe("After 12 tests pass.")
      expect(
        texts.some((text) => text.includes("11 tests pass") || text.includes("billing-platform")),
        item.name,
      ).toBe(true)
      expect(
        texts.every((text) => !text.includes("Before 11")),
        item.name,
      ).toBe(true)
    }
  })

  test("preserves GFM table facts with original byte ranges", () => {
    const block = ["| Result | Value |", "| --- | --- |", "| Tests | All 11 tests pass. |"].join("\n")
    const response = `Before 11 (open\n${block}\nAfter 12 tests pass.`
    const claims = atomizeResponseClaims(response)
    const tableFact = claims.find((claim) => claim.claim_format === "table_fact")

    expect(claims.map((claim) => claim.text)).toEqual(["Tests: All 11 tests pass.", "After 12 tests pass."])
    expect(tableFact).toMatchObject({
      raw_text: "| Tests | All 11 tests pass. |",
      table_cells: ["Tests", "All 11 tests pass."],
    })
    const [start, end] = tableFact!.source_byte_range
    expect(Buffer.from(response).subarray(start, end).toString()).toBe(tableFact!.raw_text)
  })

  test("uses marked cells for escaped pipes and fails closed for conflicting inline-code pipes", () => {
    const lines = [
      "| Field | Value |",
      "| --- | --- |",
      "| Owner | billing\\|platform |",
      "| Escaped expression | `left\\|right` |",
      "| Expression | `left|right` |",
      "| Hidden | before | `left|right` |",
      "| 状态😀 | 所有 11 个测试通过。 |",
    ]
    const response = lines.join("\n")
    const factRowIndexes = [2, 3, 6]
    const factRows = factRowIndexes.map((index) => lines[index]!)
    const factRowByteRanges = factRowIndexes.map((index) => {
      const before = lines.slice(0, index).join("\n")
      const start = Buffer.byteLength(before) + (index ? 1 : 0)
      return [start, start + Buffer.byteLength(lines[index]!)] as [number, number]
    })
    const first = atomizeResponseClaims(response)
    const second = atomizeResponseClaims(response)

    expect(first.map((claim) => claim.text)).toEqual([
      "Owner: billing|platform",
      "Escaped expression: left|right",
      "状态😀: 所有 11 个测试通过。",
    ])
    expect(first.map((claim) => claim.table_cells)).toEqual([
      ["Owner", "billing|platform"],
      ["Escaped expression", "left|right"],
      ["状态😀", "所有 11 个测试通过。"],
    ])
    expect(first.map((claim) => claim.canonical_text)).toEqual(first.map((claim) => claim.text))
    expect(first.map((claim) => claim.claim_group_id)).toEqual(second.map((claim) => claim.claim_group_id))
    expect(first.map((claim) => claim.key)).toEqual(second.map((claim) => claim.key))
    expect(first.map((claim) => claim.source_byte_range)).toEqual(factRowByteRanges)
    expect(first.map((claim) => claim.raw_text)).toEqual(factRows)
    expect(first.some((claim) => claim.raw_text.includes("Expression"))).toBe(false)
    expect(first.some((claim) => claim.raw_text.includes("Hidden"))).toBe(false)
  })

  test("maps nested list children monotonically across indentation and duplicate text", () => {
    const lines = [
      "- Parent item passes 10 tests.",
      "  continuation passes 11 tests.",
      "  - Nested item passes 12 tests.",
      "    continuation passes 13 tests.",
      "  - Nested item passes 12 tests.",
      "- Parent item passes 10 tests.",
      "  - Nested item passes 14 tests.",
    ]
    const response = lines.join("\n")
    const claims = atomizeResponseClaims(response)
    const secondParentStart = Buffer.byteLength(`${lines.slice(0, 5).join("\n")}\n`)

    expect(claims.map((claim) => claim.text)).toEqual([
      "Parent item passes 10 tests.",
      "continuation passes 11 tests.",
      "Nested item passes 12 tests.",
      "continuation passes 13 tests.",
      "Nested item passes 12 tests.",
      "Parent item passes 10 tests.",
      "Nested item passes 14 tests.",
    ])
    expect(claims.slice(0, 5).every((claim) => claim.source_byte_range[1] <= secondParentStart)).toBe(true)
    expect(claims.slice(5).every((claim) => claim.source_byte_range[0] >= secondParentStart)).toBe(true)
    expect(claims[2]!.source_byte_range).not.toEqual(claims[4]!.source_byte_range)
  })

  test("maps CRLF nested list continuations and repeated child text within the parent range", () => {
    const lines = [
      "- Parent passes 10 tests.",
      "  - Child passes 12 tests.",
      "    continuation passes 13 tests.",
      "  - Child passes 12 tests.",
    ]
    const response = lines.join("\r\n")
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual([
      "Parent passes 10 tests.",
      "Child passes 12 tests.",
      "continuation passes 13 tests.",
      "Child passes 12 tests.",
    ])
    expect(
      claims.every(
        (claim) => claim.source_byte_range[0] >= 0 && claim.source_byte_range[1] <= Buffer.byteLength(response),
      ),
    ).toBe(true)
    expect(claims[1]!.source_byte_range).not.toEqual(claims[3]!.source_byte_range)
  })

  test("backs the scan window out of a split CRLF for top-level, list, and table blocks", () => {
    const atBoundary = (prefix: string, suffix = "") => {
      const fill = 7_999 - prefix.length - suffix.length
      expect(fill).toBeGreaterThanOrEqual(0)
      return `${prefix}${"x".repeat(fill)}${suffix}\r\nAfter 99 tests pass.`
    }
    const tablePrefix = ["| Field | Value |", "| --- | --- |", "| Tests | All 11 tests pass. |", "| Padding | "].join(
      "\r\n",
    )
    const cases = [
      { name: "top-level", response: atBoundary("All 11 tests pass. "), expected: "All 11 tests pass." },
      { name: "list", response: atBoundary("- All 11 tests pass.\r\n- Padding "), expected: "All 11 tests pass." },
      { name: "table", response: atBoundary(tablePrefix, " |"), expected: "Tests: All 11 tests pass." },
    ]

    for (const item of cases) {
      const claims = atomizeResponseClaims(item.response)
      expect(
        claims.map((claim) => claim.text),
        item.name,
      ).toContain(item.expected)
      expect(
        claims.every((claim) => !claim.text.includes("After 99 tests pass.")),
        item.name,
      ).toBe(true)
    }
  })

  test("keeps list items independent across nested and consecutive lists", () => {
    const response = [
      "Before 11 (open",
      "- Owner is billing-platform.",
      "  - All 11 tests pass.",
      "- The discount cap remains 15%.",
      "After 12 tests pass.",
    ].join("\n")

    expect(atomizeResponseClaims(response).map((claim) => claim.text)).toEqual([
      "Owner is billing-platform.",
      "All 11 tests pass.",
      "The discount cap remains 15%.",
      "After 12 tests pass.",
    ])
  })

  test("ends an open list claim before following prose", () => {
    const cases = [
      {
        name: "single list",
        response: "- The discount cap remains 15% (see\nAfter 12 tests pass.",
      },
      {
        name: "nested list",
        response: "- Parent fact (open\n  - Nested fact (open\nAfter 12 tests pass.",
      },
      {
        name: "consecutive list",
        response: "- First fact (open\n- Second fact (open\nAfter 12 tests pass.",
      },
    ]

    for (const item of cases) {
      expect(
        atomizeResponseClaims(item.response).map((claim) => claim.text),
        item.name,
      ).toEqual(["After 12 tests pass."])
    }
  })

  test("treats tilde fences as hard barriers", () => {
    const response = "Before 11 (open\n~~~ts\nconst ignored = true\n~~~\nAfter 12 tests pass."

    expect(atomizeResponseClaims(response).map((claim) => claim.text)).toEqual(["After 12 tests pass."])
  })

  test("protects double and multi-backtick inline code spans", () => {
    const response = "The implementation uses ``foo(``. The verifier uses ```bar)```. All 11 tests pass."

    expect(atomizeResponseClaims(response).map((claim) => claim.text)).toEqual([
      "The implementation uses ``foo(``.",
      "The verifier uses ```bar)```.",
      "All 11 tests pass.",
    ])
  })

  test("preserves duplicate claim occurrences with unique occurrence keys", () => {
    const response = "All 11 tests pass. All 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims).toHaveLength(2)
    expect(claims.map((claim) => claim.text)).toEqual(["All 11 tests pass.", "All 11 tests pass."])
    expect(claims.map((claim) => claim.claim_group_id)).toEqual([claims[0]!.claim_group_id, claims[0]!.claim_group_id])
    expect(claims[0]!.key).not.toBe(claims[1]!.key)
    expect(claims.map((claim) => claim.claim_count)).toEqual([2, 2])
  })

  test("uses byte ranges that reverse-slice each UTF-8 raw claim", () => {
    const response = "修改完成。\n\n- 所有 11 个测试通过。\nAll 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["修改完成。", "所有 11 个测试通过。", "All 11 tests pass."])
    expect(claims.map((claim) => claim.raw_text)).toEqual(["修改完成。", "所有 11 个测试通过。", "All 11 tests pass."])
    for (const claim of claims) {
      const [start, end] = claim.source_byte_range
      const raw = Buffer.from(response).subarray(start, end).toString()
      expect(raw).toBe(claim.raw_text)
    }
  })

  test("keeps later facts after an unclosed inline backtick delimiter", () => {
    for (const delimiter of ["`", "``", "```"]) {
      const response = `Example ${delimiter}foo(\nAll 11 tests pass.`

      expect(
        atomizeResponseClaims(response).map((claim) => claim.text),
        delimiter,
      ).toContain("All 11 tests pass.")
    }
  })

  test("uses the full source for an emoji straddling the scan limit", () => {
    const prefix = "All 11 tests pass: "
    const response = `${prefix}${"a".repeat(7_999 - prefix.length)}😀`
    const claims = atomizeResponseClaims(response)

    expect(claims.length).toBeGreaterThan(0)
    for (const claim of claims) {
      const [start, end] = claim.source_byte_range
      expect(Buffer.from(response).subarray(start, end).toString()).toBe(claim.raw_text)
      expect(claim.raw_text).toBe(response.slice(0, 7_999))
    }
  })

  test("normalizes hostile final response values as a total function", () => {
    const hostile = {
      toJSON() {
        throw new Error("hostile toJSON")
      },
      toString() {
        throw new Error("hostile toString")
      },
      [Symbol.toPrimitive]() {
        throw new Error("hostile Symbol.toPrimitive")
      },
    }

    expect(() => atomizeResponseClaims(hostile)).not.toThrow()
    const first = atomizeResponseClaims(hostile)
    const second = atomizeResponseClaims(hostile)
    expect(first).toEqual(second)
    expect(first.map((claim) => claim.text)).toEqual(["[unserializable response]"])
    expect(first.map((claim) => claim.raw_text)).toEqual(["[unserializable response]"])
    expect(() => isBrokenClaimFragment(hostile)).not.toThrow()
  })

  test("fails closed when the Markdown lexer throws", () => {
    expect(() =>
      atomizeResponseClaimsWithLexerForTest("All 11 tests pass.", () => {
        throw new Error("forced lexer failure")
      }),
    ).not.toThrow()
    expect(
      atomizeResponseClaimsWithLexerForTest("All 11 tests pass.", () => {
        throw new Error("forced lexer failure")
      }),
    ).toEqual([])
  })

  test("keeps prior facts and fails closed after a lexer raw mismatch", () => {
    const response = "All 11 tests pass.\nAfter 12 tests pass."
    const claims = atomizeResponseClaimsWithLexerForTest(response, () => [
      { type: "paragraph", raw: "All 11 tests pass.\n", text: "All 11 tests pass." },
      { type: "paragraph", raw: "not the remaining source", text: "not the remaining source" },
    ])

    expect(claims.map((claim) => claim.text)).toEqual(["All 11 tests pass."])
  })

  test("fails closed for an unknown Markdown block token", () => {
    const response = "All 11 tests pass.\nAfter 12 tests pass."
    const claims = atomizeResponseClaimsWithLexerForTest(response, () => [
      { type: "paragraph", raw: "All 11 tests pass.\n", text: "All 11 tests pass." },
      { type: "unknown_block", raw: "After 12 tests pass." },
    ])

    expect(claims.map((claim) => claim.text)).toEqual(["All 11 tests pass."])
  })
})
