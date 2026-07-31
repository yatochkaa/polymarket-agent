---
name: repository-change
description: Use when implementing, debugging, refactoring, or reviewing a code change that can affect multiple files, behavior, tests, configuration, or public contracts. Enforce repository-first inspection, a minimal plan, focused edits, and evidence-based verification. Do not use for brainstorming, prose-only edits, visual-only design feedback, or simple factual questions.
---

# Repository change

## Before editing

1. Locate the real entry point, callers, tests, configuration, and nearby conventions.
2. Restate current behavior and the requested behavior.
3. Identify contracts that must remain compatible.
4. Propose the smallest coherent plan.

## MUST

- Work from inspected files, not assumed architecture.
- Preserve unrelated behavior and avoid broad formatting churn.
- Keep responsibilities at existing boundaries unless the task is explicitly architectural.
- Update tests when observable behavior changes.
- Check error paths, cancellation/cleanup where relevant, and backward compatibility.
- Use existing dependencies before adding another.
- Run the narrowest meaningful checks available, then report exact commands and results.

## SHOULD

- Prefer reversible changes and explicit ownership.
- Remove accidental complexity introduced by the change.
- Note a decision instead of silently choosing when two valid designs have materially different consequences.

## MUST NOT

- Rewrite a working subsystem to make a small change.
- create a generic abstraction for one current use case;
- fabricate files, interfaces, commands, benchmark numbers, or passing results;
- modify production resources or secrets;
- suppress checks with force flags or bypass verification without approval.

## Completion checklist

- Relevant code and tests inspected.
- Plan matched the final diff.
- Public and persistence contracts considered.
- Focused verification executed or explicitly marked unavailable.
- Remaining uncertainty stated.
