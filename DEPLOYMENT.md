# Deployment guide

## Submission demo

The repository includes a Docker image and a Render Blueprint. Docker is used
because Playwright requires Chromium and operating-system browser dependencies;
static/serverless site hosting is not sufficient for the replay worker.

### Public demo safety boundary

The Blueprint sets `PUBLIC_DEMO_READ_ONLY=true`. Keep it enabled: this public
submission surface deliberately does not pretend to have bank SSO/JWT and will
reject write capabilities and employee commands. Use only target-provided
synthetic/test credentials. Never put production bank credentials in this
deployment.

For a private live-read demo, configure these values in the hosting provider's
secret manager:

Configure these only in the hosting provider's secret manager:

- `ANTHROPIC_API_KEY`
- `MERIDIAN_OPERATOR_ID`
- `MERIDIAN_OPERATOR_PASSWORD`
- `MERIDIAN_SUPERVISOR_ID`
- `MERIDIAN_SUPERVISOR_PASSWORD`

Never add real values to `.env.example`, Git, Docker build arguments, chat
messages, or deployment logs.

### Render

1. Push the submission branch to GitHub.
2. In Render, create a new Blueprint and select this repository.
3. Render reads `render.yaml` and builds `Dockerfile`.
4. Enter only synthetic/test values for the five prompted secrets.
5. Wait until `/healthz` reports healthy.
6. Open `/customer` for the customer assistant and `/` for the read-only
   employee console.

The free plan is adequate for catalog/UI review but may be memory-constrained
for Chromium. Select a container with at least 2 GB RAM for reliable live
browser replay.

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
