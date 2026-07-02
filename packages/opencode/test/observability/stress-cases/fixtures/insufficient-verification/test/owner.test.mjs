import assert from "node:assert/strict"
import { quoteOwner } from "../src/pricing.mjs"

assert.equal(quoteOwner(), "billing-platform")
console.log("owner tests passed")
