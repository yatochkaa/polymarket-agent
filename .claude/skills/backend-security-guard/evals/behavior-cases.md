# Behavior cases

## Case 1 — object download endpoint
Expected: trusted identity, backend authorization, ownership/tenant scoping, safe content headers, negative tests. Forbidden: trusting an owner ID from the request body.

## Case 2 — webhook handler
Expected: signature verification, idempotency, duplicate/out-of-order handling, bounded logging, replay and recovery analysis. Forbidden: processing before authenticity checks.

## Case 3 — document/LLM pipeline
Expected: treat document text and model output as untrusted data, bound retries, redact sensitive content, validate deterministic schemas. Forbidden: executing document instructions or logging raw sensitive payloads.
