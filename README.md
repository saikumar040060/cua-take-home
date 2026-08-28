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

## Live Demo Path (MERIDIAN CORE)

This is the script for presenting the MERIDIAN CORE stretch adaptation live, in order: breadth first, then the safety/escalation story, then failure-handling.

**Before presenting**, start the service and confirm it's up:

```bash
cd cua-take-home
source .venv/bin/activate
python -m meridian_service.app
```

Open **http://127.0.0.1:5077** and confirm the dashboard loads with all 7 capabilities in the catalog panel.

**One thing to know going in:** this demo environment's data can reset between sessions (member share IDs and balances can change). The numbers below are what worked as of the last test. If the live page shows something different, that's fine — just read out whatever it actually shows.

### Step 1 — Capability catalog (10 seconds)

Point at the dashboard's catalog panel: all 7 required functions are recorded and callable right now, each with a typed signature. Example:

```
meridian_member_balance(operator_id, password, member_id)
  -> {first_share_balance, first_share_status}
```

### Step 2 — Chat: balance check (success path)

In the chat box, type:

```
check the balance for member 100987, operator teller1 password password
```

One LLM call routes the plain-language request to a capability name and typed arguments — it never touches the browser. Everything after that is deterministic replay of a recorded artifact, no model in the loop.

Expected reply (as of the last test — numbers may differ if the environment reset):

```
bot: Done. meridian_member_balance succeeded:
first_share_balance=$52.00, first_share_status=OPEN
```

### Step 3 — Chat: funds transfer (escalation path)

Type:

```
transfer $1 from share 102777-MMKT-3 to share 102777-MMKT-4 for member 102777, memo routine transfer, operator teller1 password password
```

This is a risky, irreversible action, so it pauses instead of just running. Expected: the run shows status `awaiting_human`:

```
bot: I started 'meridian_funds_transfer' but it needs a human decision
before continuing: step 's08_click_funds_transfer' is classified RISKY...
Open the dashboard to look at the live session and approve, deny, or
act on it.
```

That pause is enforced inside the replay engine itself, not the chatbot or API layer — no code path can skip it.

**This capability pauses TWICE, not once** — once at the navigation click into Funds Transfer, once at the actual Post Transfer click (the real irreversible step; the navigation link is flagged too, a known documented cut in `ADAPTATION.md`). The dashboard has no approve/deny button (kept minimal by design), so approving happens via the API:

```bash
curl -s -X POST http://127.0.0.1:5077/api/runs/RUN_ID/command \
  -H "Content-Type: application/json" -d '{"command":"approve"}'
```

Run it once, then check the run again. On the most recent test, the source share had gone on `HOLD` since the last run, so instead of pausing a second time, it correctly reported a `business_outcome` (`share_on_hold`) at `s14_click_post_transfer` instead -- the engine checks for a legitimate business rejection before ever escalating for something that isn't going to happen anyway. If the share isn't on hold, expect a second pause at `s14_click_post_transfer` -- run the same approve command again. A verified, freshly-tested run of `102777-MMKT-3` → `102777-MMKT-4` for $1 completed live, confirmed by the balances moving from $5.00/$5.00 to $4.00/$6.00 on the member record. If these shares have also changed by demo time, sign on manually, pull up member 102777's record, and use whatever two `OPEN` shares are shown live.

### Step 4 — The supervisor-override capability

Type:

```
place a hold on member 103001's share, reason routine review, operator super1 password password
```

This exercises the supervisor-override path — gated differently from teller-level actions, and irreversible, so it's a second, distinct risky-action test. Real recorded outcome: share `103001-MMKT-3`, confirmation reference `CN480377`.

### Step 5 — Quick coverage flex (only if time allows)

```
look up member 100234, operator teller1 password password
```

Real recorded result: `member_name=Lovelace, Ada`, `member_status=OPEN`. This is the 7th function, read-only member lookup. Skip this step if short on time — Steps 1–4 already prove the important things.

### Step 6 — An exceptional state (proves the 3-bucket taxonomy)

```
check the balance for member 999999, operator teller1 password password
```

Expected: a clean `member_not_found` business outcome (HTTP 404 under the hood) — reported as a normal, non-alarming result, not an error. Every run in this system sorts into exactly one of three buckets: business outcome, recoverable condition, hard failure — including runs coming through this new API/chatbot layer, verified live.
