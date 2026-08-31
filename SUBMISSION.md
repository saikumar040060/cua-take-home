# MERIDIAN Assist — submission guide

## What this product is

MERIDIAN Assist lets a bank customer request approved servicing operations in
plain language while giving bank employees one place to review exceptions,
approve or deny sensitive steps, take over a live session, and inspect the
redacted execution history.

The core innovation is **discover once, replay deterministically**. A supervised
LLM discovery agent learns a legacy browser workflow and records a typed,
versioned capability. Customer requests use one LLM call, when configured, only
to select a published capability and collect typed inputs; an artifact relevance
ranker is the provider fallback. The browser execution itself has no LLM in the
loop.

Two backend systems are driven by the same unmodified core — the live
MERIDIAN CORE console (7 capabilities) and the bundled self-hosted
mock_app (123 members, 10 capabilities including loans, bill pay, card
lock, and transaction history) — 17 recorded capabilities total, all
listed in one catalog and callable through the same customer chat,
employee console, and direct API.

## Five-minute review path

1. Open `/customer` and log in as a member (demo password `password`;
   e.g. `100987` for the live MERIDIAN CORE backend, or `20001` for the
   bundled mock_app backend — the latter needs `python -m mock_app.app`
   running and is faster). The home page shows live account cards read by
   real deterministic replay; the chat opens from the floating launcher.
2. Show that the assistant is session-scoped: `member_id` never appears
   in the chat tool schema, is never accepted from chat text, and each
   member's tool catalog covers only their own backend system. Asking
   about another member's account fails cleanly instead of answering.
3. Open `/` and show the published capabilities across both systems in
   the employee console — each shown as a plain-language card (category,
   read-only/write, approval requirement, risk), technical ID and
   signature tucked behind a "Technical details" disclosure. Console is
   viewable read-only by anyone; sign in as an employee (`teller1` or
   `super1`, password `password`) to unlock approve/deny/resume/abort.
4. Explain that customer-visible schemas never include the legacy operator
   ID or password; credentials are injected by infrastructure after routing.
5. Run a balance inquiry to show deterministic replay and structured output.
6. Request a transfer against the bundled synthetic bank. Show that replay pauses
   only on the actual commit step; approving/denying it now requires the
   employee session from step 3, and the confirmation dialog names the
   capability and explains the impact before it can be approved.
7. Open the run detail (a tabbed drawer: Overview, Timeline, Evidence,
   Technical logs) to show the plain-language summary first, then the
   per-step screenshot trail for the escalated run, then redacted raw
   evidence.
8. Show the "Engine fixes found through real use" section of `README.md` —
   four real locator/identity bugs surfaced by re-recording against both
   targets, each with a verified fix — and `ARCHITECTURE.md` for the
   production target design.

## Safety and reliability implemented in the submission

- corrected commit-step risk classification and human approval
- server-bound legacy credentials, excluded from chat and public APIs
- customer login with session-bound member identity: the routing model
  never sees or accepts member_id, and each member's chat is scoped to
  their own backend system (covered by the `tests/test_customer_auth.py`
  suite)
- identity-anchored locator resolution: a nonexistent or foreign
  account/share ID fails cleanly through the three-bucket triage instead
  of silently returning another row's data
- employee login gating approve/deny/resume/abort and console commands;
  anonymous visitors get a clean 401, not a silent write, and can still
  view the catalog and run history read-only
- a per-step screenshot trail for any run that needed a human decision,
  so an employee can see exactly what the operator screen showed at each
  action, not just at the failure point
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

The customer and employee logins are both a demo convention (a fixed
shared password against a known-identity list), not real authentication —
what they demonstrate is the *authorization* property built on top: once
a session is bound to a member, no code path lets that session's chat act
on any other member's account; once a session is bound to an employee,
anonymous visitors can view the console but cannot approve, deny, resume,
or abort a run. `PUBLIC_DEMO_READ_ONLY=true` blocks non-synthetic writes;
the explicitly enabled public-demo exception targets only the co-located fake
bank and still requires an employee decision. Real bank OIDC/JWT,
per-employee RBAC (e.g. distinguishing what a teller vs. a supervisor may
approve), and step-up MFA for sensitive actions remain future work.

## Verification

Run:

```bash
pytest -q
```

Then follow `DEPLOYMENT.md` for Docker and Render. Keep
`PUBLIC_DEMO_READ_ONLY=true` on every public URL and never configure real-bank
credentials on a deployment that enables synthetic writes.
