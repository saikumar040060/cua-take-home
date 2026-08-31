# Deployment guide

## Submission demo

The repository includes a Docker image and a Render Blueprint. Docker is used
because Playwright requires Chromium and operating-system browser dependencies;
static/serverless site hosting is not sufficient for the replay worker.

### Public demo safety boundary

The Blueprint sets `PUBLIC_DEMO_READ_ONLY=true`. Keep it enabled: this public
submission surface deliberately does not pretend to have bank SSO/JWT and will
reject write capabilities and employee commands. It also sets
`PUBLIC_DEMO_SYNTHETIC=true`, starts the bundled synthetic legacy bank inside
the container, and uses an auditable deterministic router for the supported
read-only chat intents. The public deployment therefore needs no LLM key and
no private bank credentials.

If `ANTHROPIC_API_KEY` is added to the Render service's secret environment,
the chatbot automatically switches to LLM tool selection over the approved
artifact catalog. If the provider is unavailable, it falls back to local
artifact relevance ranking. `/readyz` reports the active `router_mode`.

For a separate private live-bank demo, set `PUBLIC_DEMO_SYNTHETIC=false` and
configure these values only in the hosting provider's secret manager:

- `ANTHROPIC_API_KEY`
- `MERIDIAN_OPERATOR_ID`
- `MERIDIAN_OPERATOR_PASSWORD`
- `MERIDIAN_SUPERVISOR_ID`
- `MERIDIAN_SUPERVISOR_PASSWORD`

Never add real values to `.env.example`, Git, Docker build arguments, chat
messages, or deployment logs.

Employee console login (`teller1`/`super1`, password `password`) is a fixed
demo convention, not a secret to configure — it's the same identity pair
`MERIDIAN_OPERATOR_ID`/`MERIDIAN_SUPERVISOR_ID` name, hardcoded for the demo
rather than pulled from an env var.

### Render

1. Push the submission branch to GitHub.
2. In Render, create a new Blueprint and select this repository.
3. Render reads `render.yaml` and builds `Dockerfile`.
4. Wait until `/readyz` reports ready with `synthetic_backend: true`.
5. Open `/customer` for the customer assistant and `/` for the employee
   console (viewable read-only by anyone; sign in at `/employee/login` for
   approve/deny/resume/abort — those still fail closed under
   `PUBLIC_DEMO_READ_ONLY=true` regardless of login).

The public build serves its synthetic account overview directly from the
bundled data set, then launches Chromium only for assistant capability runs.
Use separate replay workers and at least 2 GB RAM per worker for sustained or
concurrent production traffic.

### Local container

```bash
docker build -t meridian-automation-demo .
docker run --rm -p 10000:10000 --env-file .env meridian-automation-demo
```

Then open:

- Customer: `http://127.0.0.1:10000/customer`
- Employee: `http://127.0.0.1:10000/`
- Health: `http://127.0.0.1:10000/healthz`
- Readiness: `http://127.0.0.1:10000/readyz`

## Production changes

The demo keeps run state in memory and evidence on the container filesystem.
Production requires durable job/state storage, shared rate limiting and
idempotency, encrypted evidence storage, an immutable audit store, bank OIDC,
separate replay workers, workload identity, network egress policy, backups,
disaster recovery, and security/compliance review.

For the private interview demo of approval/deny behavior, run locally with
`PUBLIC_DEMO_READ_ONLY=false`; the replay engine will still stop at each actual
commit action and wait for the employee decision.
