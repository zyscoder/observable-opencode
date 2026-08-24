# Node Semantics

Translate recorded nodes into task-level questions before assigning semantic taint. **No node type is inherently defective.** Kind, component, status, and temporal position locate evidence; the bound expectation and semantic content determine whether a defect exists.

| Node family | Task-level questions |
|---|---|
| User request | What did the user explicitly require, prohibit, correct, or leave open? Which later expectation comes from this text rather than analyst preference? |
| Context | Which instructions, prior turns, catalog entries, Artifacts, and tool results were actually available? Was current information preserved, omitted, stale, or conflicting? |
| LLM call | What question-relevant input reached the model, and what completion or action proposal came back? Do not infer hidden reasoning that the Trace does not record. |
| Decision | Which option did the Agent select or reject, on what recorded basis, and did it preserve the governing expectation? A decision is a root candidate only if its relevant inputs were clean or insufficient to dictate the same defect. |
| Tool | What command or operation was requested, what did it actually return, and how was that result consumed? Success and failure statuses are facts, not blame assignments. |
| Skill | Was a matching Skill exposed, loaded, and invoked? Did its visible description provide a usable trigger? For omission, locate exposure and the first post-exposure stage that failed to realize it. |
| MCP | Was the server or capability exposed and callable, was a call produced, and was its result consumed correctly? Separate availability, selection, execution, and interpretation. |
| Subagent | What task and evidence were delegated, what result returned, and how did the parent use it? Delegation itself is neither proof of coverage nor a defect. |
| Mutation | What file, configuration, prompt, or state changed; what invariant governed it; and what semantic difference did the Artifact establish? Distinguish requested edits from unintended effects. |
| Verification | Which claim was checked, by what recorded action and result, and did the later conclusion match that evidence? Missing verification and misinterpreted verification are different defects. |
| Response | Which conclusion, caveat, or omission reached the user, and which upstream decision or evidence supports it? A response often propagates an earlier defect but can independently introduce a false claim. |
| Compaction | Which facts survived summarization, which were dropped or transformed, and did a later action rely on the compacted form? Do not blame compaction merely because it occurred. |
| Lifecycle | Did start, resume, cancel, retry, finalize, or exit behavior alter the relevant evidence window or leave an expected action unrealized? Lifecycle order alone does not prove causation. |
| Artifact | What semantic content does the declared, hash-verified Artifact establish, which nodes reference it, and is it current for the analyzed turn? A missing or unverifiable Artifact creates an unknown, not a presumed fact. |

## Task-Language Translation

Replace unexplained labels with component, content, and consequence. For example:

> The local compiler returned success for the generated command. The next decision treated that result as sufficient verification even though the preserved request required the project build workflow.

This identifies what the Tool established and where the semantic interpretation changed. It does not claim that `tool.result` is defective merely because it appears near the final response.

For every family, inspect recorded payload, scope, source refs, eligible edges, and verified Artifacts before deciding. If the relevant content is absent, say `unknown`; do not fill the gap from typical Agent behavior.
