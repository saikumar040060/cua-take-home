# MERIDIAN Assist — submission guide

## What this product is

MERIDIAN Assist lets a bank customer request approved servicing operations in
plain language while giving bank employees one place to review exceptions,
approve or deny sensitive steps, take over a live session, and inspect the
redacted execution history.

The core innovation is **discover once, replay deterministically**. A supervised
LLM discovery agent learns a legacy browser workflow and records a typed,
versioned capability. Customer requests use one LLM call only to select a
published capability and collect typed inputs. The browser execution itself has
no LLM in the loop.

## Five-minute review path

1. Open `/customer` and show the customer assistant.
2. Open `/` and show seven published capabilities in the employee console.
3. Explain that customer-visible schemas never include the legacy operator ID
   or password; credentials are injected by infrastructure after routing.
4. Run a balance inquiry to show deterministic replay and structured output.
5. In the private/local demo, request a transfer. Show that replay pauses only
   on `Post Transfer`, then approve or deny it in the employee console.
6. Open the run detail to show correlated status and redacted evidence.
7. Show `ARCHITECTURE.md` and explain that the discovery LLM is an internal
   Capability Studio component, never part of a live banking transaction.

## Safety and reliability implemented in the submission

- corrected commit-step risk classification and human approval
- server-bound legacy credentials, excluded from chat and public APIs
- idempotency keys for write operations
- request IDs, structured logs, security headers, and health/readiness checks
- endpoint rate limiting and short-lived capability-catalog caching
- circuit breaker and safe fallback around LLM intent routing
- try/catch boundaries with fail-closed customer messages
- redacted evidence and no raw secret/identifier echo in chat responses
- container packaging with a public read-only deployment mode

## What is deliberately not claimed

This is a production-shaped submission, not a certified real-bank deployment.
Real launch requires bank OIDC/JWT, fine-grained authorization, durable workflow
and queue storage, encrypted evidence and immutable audit storage, isolated
browser workers, shared rate limits/idempotency, unknown-effect reconciliation,
observability/SIEM integration, resilience testing, and security/compliance
approval. The target design for those pieces is in `ARCHITECTURE.md`.

## Verification

Run:

```bash
pytest -q
```

Then follow `DEPLOYMENT.md` for Docker and Render. Keep
`PUBLIC_DEMO_READ_ONLY=true` on every public URL.
