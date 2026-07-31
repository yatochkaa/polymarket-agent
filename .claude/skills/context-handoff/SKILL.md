---
name: context-handoff
description: Create a compact, evidence-based handoff before clearing context, switching sessions, pausing a long task, or transferring work to another agent. Capture current state, decisions, risks, exact next step, and verification without repeating the whole conversation.
disable-model-invocation: true
---

# Context handoff

Produce a concise Markdown handoff using only verified conversation and repository evidence.

## Required sections

1. **Goal** — one paragraph.
2. **Current state** — completed, in progress, and not started.
3. **Evidence** — relevant files, symbols, commands, and observed results.
4. **Decisions** — chosen approach and rejected alternatives with short reasons.
5. **Risks and unknowns** — facts separated from assumptions.
6. **Next action** — exactly one concrete next step.
7. **Resume prompt** — a short prompt that another session can use immediately.

## Rules

- Do not paste large code blocks, logs, or the full chat.
- Do not claim checks were run when they were not.
- Preserve exact paths and command results when known.
- Mark unstable facts that must be re-checked.
- Target roughly 500–900 words unless the task genuinely needs less.
