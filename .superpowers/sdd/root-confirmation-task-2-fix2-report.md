# Root Confirmation Stability Task 2 Fix Wave 2 Report

Base commit: `833847f1f`

## Scope

Closed the Task 2 focus canonicalization review finding only. Analysis remains
offline, passive, and read-only; no dependency or Task 3 behavior was added.

## Root Cause

`normalize_active_focus_text()` used NFKC normalization, whitespace splitting,
and case folding. `GlobalCandidateJudgeRequest.__post_init__()` also stripped
the supplied focus text. Those transformations allowed distinct defect texts to
share an equality identity and focus hash.

## RED

Updated `test_global_judge.py` before production code and ran:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`

Result: 29 tests ran; 5 failures. The canonicalizer converted CRLF/CR into
spaces and accepted `x²`/`x2`, `ParseJSON`/`parsejson`, whitespace changes,
and the `ﬁ` compatibility character/`fi` pair as equivalent.

## Implementation

- `normalize_active_focus_text()` now replaces only `CRLF` and `CR` with `LF`.
- `active_focus_text_sha256()` continues to hash that same canonical function.
- Request construction preserves supplied focus text exactly; the nonblank
  validation check does not mutate it.
- Existing request validation remains the shared boundary for prompt/cache,
  replay/validator, fallback, and analyzer consumption.

## Tests

Added focused coverage for:

- CRLF/CR-to-LF canonicalization and stable hashes;
- rejection of superscript, case, whitespace, and Unicode compatibility-text
  changes by both canonical equality and hashing;
- request-level rejection of those lossy changes;
- request-level acceptance of CRLF/LF-equivalent focus text.

## Final Verification

1. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`
   - `Ran 30 tests` / `OK`.
2. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py -v`
   - `Ran 86 tests` / `OK`.
3. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_analyzer.py -v`
   - `Ran 80 tests` / `OK`.
4. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
   - `Ran 633 tests in 3.146s` / `OK`.
5. `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-fix2-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`
   - exit status `0`.
6. `git diff --check`
   - exit status `0`.
