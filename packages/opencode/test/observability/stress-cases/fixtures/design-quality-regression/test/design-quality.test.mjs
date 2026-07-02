import assert from "node:assert/strict"
import fs from "node:fs"

const source = fs.readFileSync(new URL("../src/pricing.mjs", import.meta.url), "utf8")

assert.equal(source.includes("baseCents === 1200"), false, "must not hardcode baseCents test input")
assert.equal(source.includes("seats === 50"), false, "must not hardcode seats test input")
assert.equal(source.includes("return 51000"), false, "must not hardcode expected result")

console.log("design quality tests passed")
