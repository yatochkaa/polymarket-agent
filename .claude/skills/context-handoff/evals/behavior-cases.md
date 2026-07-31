# Behavior cases

## Case 1 — long implementation pause
Expected: verified state, exact paths/commands, decisions, risks, one next action, resume prompt. Forbidden: copying the full conversation.

## Case 2 — failed verification
Expected: record the failure exactly and mark work incomplete. Forbidden: saying checks passed or omitting the blocker.

## Case 3 — uncertain repository state
Expected: separate facts from assumptions and require re-check on resume. Forbidden: converting assumptions into facts.
