# AGENTS.md

## Project context

Build a personal analytical layer over Polymarket: one user, one VPS, read-only data. This is not a product, SaaS, clone, or copy-trading service.

Priorities:
1. Personal terminal replacing a paid analytics subscription; build regardless of whether an edge exists.
2. Test whether tennis traders have a copyable edge; `NO-GO` is an expected valid outcome.
3. Produce an open market-integrity dataset as the guaranteed deliverable.

## Hard infrastructure facts

Do not assume otherwise:

- Polymarket CLOB V2 starts from `2026-04-28`.
- Main CLOB V2 contract: `0xE111180000d2663C0091e4f400237545B87B996B`.
- Neg-Risk contract: `0xe2222d279d744050d28e00520010520000310F59`.
- Use `polymarket-client` from `Polymarket/py-sdk`.
- Never use `py-clob-client`; it is archived and non-working.
- `/orderbook-history` is dead since `2026-02-20` and silently returns empty data.
- Historical order book data only exists if we collect it ourselves.
- Collateral is `pUSD`, not `USDC.e`.
- There is no on-chain cancellation; there is operator `pauseUser`.
- `expiration` was removed from the EIP-712 signature but remains in the `POST /order` body.
- Fee formula: `fee = C * feeRate * p * (1 - p)`, where `C` is the number of shares.
- Fee rates: Crypto `0.07`, Sports `0.05`, Politics `0.04`, Geopolitics `0`.
- Makers never pay fees.
- Displayed price is `mid`; if spread is greater than `$0.10`, displayed price is the last trade price.

## Tech stack

Use:

- Python 3.12
- `polymarket-client`
- `httpx`
- `websockets`
- `duckdb`
- `pyarrow`
- `streamlit`
- `python-telegram-bot`
- `pydantic`
- `systemd`

Do not introduce at the start:

- Docker Compose
- Kafka
- Kubernetes
- Airflow
- Microservices
- Postgres

## Methodology rules

Violating these requires rework:

- Observation unit is the event/match, not the trade.
- Use clustered standard errors.
- Rank by posterior mean with shrinkage, not raw in-sample performance.
- Apply BH-FDR for multiple comparisons.
- Every report must include a shrinkage share line.
- Edge decision criterion: `gross_delta - cost > 2.5 * sqrt(SE_edge^2 + SE_cost^2)`.
- The criterion uses the sum under the square root, not `max`.
- Supported outcomes are exactly: `GO`, `NO-GO`, `UNDECIDABLE`.
- Use `UNDECIDABLE` when `bracket_width > gross_delta`.
- All fee numbers must come only from `fees.py`; do not hardcode fees anywhere else.
- Exclude own addresses via a self-exclusion list.

## Coding rules

- Produce working code, not sketches.
- Include types, error handling, and docstrings.
- If data is insufficient, say so directly; do not invent missing facts.
- Separate statements into:
  - confirmed by documentation,
  - likely,
  - assumption.
- Do not present copy-trading as the main strategy; it is a hypothesis to test, not an accepted premise.

PREREGISTRATION.md — read-only. Не предлагать изменения критериев,
порогов и исходов. Если считаешь критерий неверным — сказать один раз
и продолжить работу по нему.

## Обязательное завершение любой задачи
В конце сессии дописать в PROGRESS.md: дату, что сделано,
что не сделано и почему. Если файла нет — создать.
Не коммитить и не пушить. Решения, которые не можешь
принять сам, дописывать в DECISIONS_NEEDED.md.