---
name: backend-security-guard
description: Use when adding or reviewing backend endpoints, authentication, authorization, database access, tenant-scoped data, file uploads, webhooks, background jobs, external API calls, secrets, logging, caching, exports, or administrative actions. Apply threat-focused checks and fail-closed behavior. Do not use for frontend styling, harmless copy changes, or isolated pure calculations with no trust boundary.
---

# Backend security guard

Treat every request, document, event, callback, job payload, external response, and model output as untrusted until validated.

## MUST

- Identify the actor, trusted identity source, resource, action, and authorization decision.
- Authenticate before authorization and deny by default.
- Enforce authorization on the backend; UI visibility is not a security control.
- Scope every data access path to the authorized owner or tenant when such boundaries exist.
- Validate at trust boundaries and use allowlists for identifiers, states, file types, redirects, and externally supplied destinations where applicable.
- Use parameterized database access and bounded pagination or result limits.
- Protect secrets from source code, errors, logs, traces, fixtures, prompts, and client responses.
- Make webhooks and retried jobs authentic, idempotent, replay-aware, and safe for duplicate or out-of-order delivery.
- Bound file size, parsing work, retries, concurrency, and external-call timeouts.
- Redact sensitive fields while preserving enough metadata for debugging and audit.
- Add negative tests for unauthorized, cross-owner/cross-tenant, malformed, duplicate, and replay cases relevant to the change.

## REVIEW WHEN APPLICABLE

- IDOR/BOLA and mass assignment.
- SQL/command/template/path injection.
- SSRF, unsafe redirects, archive traversal, and untrusted file parsing.
- CSRF, CORS, cookies, token storage, session invalidation, and rate limiting.
- Cache-key and object-storage namespace isolation.
- Race conditions, double processing, stale authorization, and privilege escalation.
- Sensitive model inputs/outputs and prompt injection from documents or retrieved content.

## MUST NOT

- Trust identity, role, price, entitlement, ownership, or tenant from a request body alone.
- Log credentials, raw tokens, full sensitive documents, or unrestricted model output.
- Disable validation or security checks to make tests pass.
- perform production remediation, rotate keys, or change access policies without explicit approval;
- claim security is complete: report scope, assumptions, residual risk, and missing tests.

## Output

List findings by severity and evidence. Prefer a small concrete fix over a generic security lecture.
