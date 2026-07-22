import { describe, expect, test } from "bun:test"
import { atomizeResponseClaims } from "../../src/observability/claim-atomization"

describe("claim atomization", () => {
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
        response: "Before 11 (open\n| Result | All 11 tests pass. |\nAfter 12 tests pass.",
        expected: ["Result: All 11 tests pass.", "After 12 tests pass."],
      },
      {
        name: "blockquote",
        response: "Before 11 (open\n> Quoted 11 tests pass.\nAfter 12 tests pass.",
        expected: ["After 12 tests pass."],
      },
    ]

    for (const item of cases) {
      expect(atomizeResponseClaims(item.response).map((claim) => claim.text), item.name).toEqual(item.expected)
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
      expect(atomizeResponseClaims(item.response).map((claim) => claim.text), item.name).toEqual([
        "After 12 tests pass.",
      ])
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

    for (const claim of claims) {
      const [start, end] = claim.source_byte_range
      const raw = Buffer.from(response).subarray(start, end).toString()
      expect(raw).toBe(claim.raw_text)
    }
  })
})
