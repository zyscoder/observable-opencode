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

  test("uses stripped-source byte ranges after markdown scaffold removal", () => {
    const response = "## Summary\n```ts\nconst ignored = true\n```\n修改完成。All 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((claim) => claim.text)).toEqual(["修改完成。", "All 11 tests pass."])
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength("修改完成。")])
    expect(claims[1]!.source_byte_range).toEqual([
      Buffer.byteLength("修改完成。"),
      Buffer.byteLength("修改完成。All 11 tests pass."),
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
})
