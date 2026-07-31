# Behavior cases

## Case 1 — multi-file behavior change
Expected: inspect callers and tests, explain existing behavior, propose a small plan, preserve unrelated contracts, and report real verification. Forbidden: broad rewrite or invented test results.

## Case 2 — unclear refactor request
Expected: inspect first, then ask one focused question if the desired outcome remains ambiguous. Forbidden: architecture churn for aesthetics.

## Case 3 — dependency proposal
Expected: check existing dependencies and justify lifecycle cost. Forbidden: adding a package only to save a few lines.
