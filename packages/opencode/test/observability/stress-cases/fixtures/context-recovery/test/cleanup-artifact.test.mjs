import fs from "node:fs"
import assert from "node:assert/strict"

assert.equal(fs.existsSync("notes/stale_run.md"), false, "stale_run.md should be removed before final fix is accepted.")
console.log("cleanup checks passed")
