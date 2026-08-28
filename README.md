# Computer-Use Automation System

A small, end-to-end implementation of the record-once / replay-many model for
operating legacy UIs that have no API:

1. An **LLM-driven discovery agent** (Anthropic tool-calling + Playwright)
   completes a natural-language goal against a live web app.
2. The successful run is serialized into a **typed, versioned capability
   artifact** (Pydantic) — parameterized steps, fallback locator chains,
   checkpoints, declared business outcomes and recovery rules.
3. A **deterministic replay engine** executes the artifact with **no model in
   the loop**, classifying every run into success / business outcome / hard
   failure, with recoverable conditions handled in-flight.
4. A **policy gate** (allowlist + risky-action treatment + redaction) fronts
   every action, and a **human handoff console** can take over the live
   browser session and hand it back.

The target is a bundled mock "legacy" bank back-office app (server-rendered
tables, no test IDs, injectable runtime faults). See `REPORT.md` for design
reasoning and `evidence/` for recorded runs.

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # skip if Chromium is already available
cp .env.example .env                 # then put your Anthropic API key in .env
```

The API key is needed **only for discovery** in the original take-home flow
below. Replay, the mock app, and the whole test suite run without any key or
network access. The one exception is the MERIDIAN CORE chatbot further down
this README -- it also calls the API live to route each chat message, so make
sure your key is in `.env` before using its chat box.

If you cannot (or don't want to) let Playwright download a browser, point
`CUA_CHROMIUM_PATH` in `.env` at an existing Chromium/Chrome binary.

## Demo path

Terminal 1 — start the mock bank back-office app:

```bash
python -m mock_app.app                       # serves http://127.0.0.1:5000
```

Terminal 2 — run the LLM discovery agent on a goal, producing an artifact:

```bash
python -m cua discover \
  --spec specs/open_sub_account.json \
  --out artifacts/open_sub_account.json \
  --approve-risky        # operator pre-approval for the form submission (logged)
```

Replay that artifact deterministically (no LLM), with different inputs:

```bash
python -m cua replay artifacts/open_sub_account.json \
  --param member_id=10456 --param product_type="Money Market" \
  --param nickname="Emergency fund" --param initial_deposit=50.00 \
  --confirm-risky        # caller sign-off for the risky submit step
```

Useful variations:

```bash
# Business outcome: unknown member — reported as an outcome, not a failure
python -m cua replay artifacts/open_sub_account.json \
  --param member_id=99999 --param product_type="Money Market" \
  --param nickname=x --param initial_deposit=50.00 --confirm-risky

# Business outcome: validation rejection (deposit below the $5 minimum)
python -m cua replay artifacts/open_sub_account.json \
  --param member_id=10456 --param product_type="Money Market" \
  --param nickname=x --param initial_deposit=1.00 --confirm-risky

# Recoverable condition: restart the app with MOCK_CHAOS=interstitial and
# replay — the session-notice interstitial is dismissed via a recorded
# recovery rule and the run still succeeds.
# Hard failure: restart with MOCK_CHAOS=broken — replay stops with a
# structured failure (step, expected, observed) + screenshot + DOM snapshot.

# Risky step without --confirm-risky: the run pauses and hands the live
# session to the operator console (approve / deny / act manually / resume).

# List recorded capabilities the way a calling agent would see them:
python -m cua catalog
```

Every run (discovery and replay) writes `runs/<run_id>/` with an
`events.jsonl` structured log, screenshots, and `result.json` /
`artifact.json`. The committed `evidence/` directory contains a real
discovery run and replay runs for each outcome class — see
`evidence/README.md`.

## Running without live services

No external services are used at all: the target app is local, and replay
requires no key. To verify the system without an Anthropic key, run the test
suite — it exercises discovery (with a scripted decider), recording, replay
across all outcome classes, the policy gate, redaction, and handoff against
the real app and a real browser:

```bash
pytest -q
```

## Layout

```
mock_app/          the legacy-style target app (Flask), fault injection via MOCK_CHAOS
cua/schema.py      capability artifact schema (the core data model)
cua/browser.py     session, semantic page snapshot, locator chains + resolution
cua/discovery/     goal spec, Anthropic decider, agent loop, recorder
cua/replay/        deterministic engine + result contract
cua/safety/        policy gate (allowlist, risk classes), redaction
cua/escalation/    intervention requests + operator console (handoff)
specs/             goal specifications (the operator-declared contract)
artifacts/         recorded capabilities
evidence/          committed example runs (discovery + replays)
tests/             pytest suite (runs hermetically, no API key)
meridian_service/  capability API + chatbot + dashboard for MERIDIAN CORE
```

## MERIDIAN CORE adaptation

The same core also drives a second, unrelated live target — MERIDIAN CORE, a
credit-union servicing console at `web-sample.interface-hiring.com` — with no
changes to the discovery loop, artifact schema, or replay engine themselves.
See `ADAPTATION.md` for what adapting to it actually required.

Record a capability against the live target (needs real network access —
this will not work from a sandboxed/offline environment):

```bash
python -m cua discover \
  --spec specs/meridian_core/member_balance.json \
  --out artifacts/meridian_core/member_balance.json \
  --policy policy_meridian.json \
  --approve-risky
```

All 7 required functions are recorded and verified against the live target,
in `artifacts/meridian_core/`: `sign_on`, `member_balance`, `funds_transfer`,
`place_account_hold` (supervisor override), `member_inquiry`, `open_new_share`,
and `update_member_info`.

Run the capability API + chatbot + dashboard:

```bash
python -m meridian_service.app
```

Then open **http://127.0.0.1:5077** — browse the capability catalog, type a
plain-language request into the chat box (e.g. "check the balance for member
100987, operator teller1 password password"), and watch it route to the right
capability and run the real deterministic replay. Run history and evidence
are visible on the same page. `GET /api/capabilities` and
`POST /api/capabilities/<id>/invoke` are the callable API a calling agent
would use directly, with no knowledge of the underlying UI.
