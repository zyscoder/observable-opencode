import assert from "node:assert/strict"
import { renewalQuote } from "../src/pricing.mjs"

const combined = renewalQuote({ baseCents: 1200, seats: 50, loyaltyYears: 4 })
assert.equal(combined, 51000)

console.log("pricing tests passed")
