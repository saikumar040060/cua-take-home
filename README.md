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

Two targets are driven by the same unmodified core, 17 recorded
capabilities in total:

- **mock_app** (bundled, self-hosted) — a legacy-style bank back-office
  app (server-rendered tables, no test IDs, injectable runtime faults),
  extended to 123 synthetic members and 10 recorded capabilities: member
  lookup, per-account balance, funds transfer, contact-info update,
  account close, card lock, loan application, bill pay, transaction
  history, and a restricted account hold. See `REPORT.md` for design
  reasoning and `evidence/` for recorded runs.
- **MERIDIAN CORE** (live, external) — the credit-union servicing console
  at `web-sample.interface-hiring.com`, 7 recorded capabilities (see the
  adaptation section below).

The submission build adds two product surfaces around that core:

- `/customer` — customer banking site with login; an LLM tool router (with an
  artifact-ranked fallback) routes intent only, and each logged-in member's
  chat is scoped to their own account on their own backend system.
- `/` — bank-employee operations console with login, intervention actions,
  run history, correlated events, a per-step visual trail for escalated
  runs, and the approved capability catalog across both systems.

See `ARCHITECTURE.md` for the banking-grade target architecture and
`DEPLOYMENT.md` for the container deployment path.

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

For the MERIDIAN service, also configure the server-owned operator and
supervisor credentials shown in `.env.example`. Customers never provide these
credentials and the routing LLM never receives them.

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
mock_app/          the legacy-style target app (Flask), 123 members,
                   fault injection via MOCK_CHAOS
cua/schema.py      capability artifact schema (the core data model)
cua/browser.py     session, semantic page snapshot, locator chains + resolution
cua/discovery/     goal spec, Anthropic decider, agent loop, recorder
cua/replay/        deterministic engine + result contract
cua/safety/        policy gate (allowlist, risk classes), redaction
cua/escalation/    intervention requests + operator console (handoff)
specs/             goal specifications (mock_app/ and meridian_core/)
artifacts/         recorded capabilities (mock_app/ and meridian_core/)
evidence/          committed example runs (discovery + replays)
tests/             pytest suite (runs hermetically, no API key)
meridian_service/  customer site + capability API + employee console,
                   multi-backend (mock_app and MERIDIAN CORE)
```

## Engine fixes found through real use

Re-recording and replaying capabilities against both targets surfaced
four real engine bugs, each verified live and fixed (with the fix
exercised by replay across multiple inputs):

1. **Wrong-column anchoring in data tables.** The recorder anchored a
   data-cell read on whatever text sat in the adjacent column — right for
   label:value forms, wrong for multi-column tables where every column is
   data. A per-share balance check silently returned a different share's
   figures. Fixed by re-anchoring the locator on whichever cell in the
   row matches a declared input's value (the row's real identity), and by
   making the replay engine substitute `{param}` templates into locator
   text (it never had).
2. **Silent wrong-row fallback.** When an identity-anchored locator found
   zero rows (e.g. a share_id belonging to a different member), the
   positional `dom_path` fallback still matched *some* row and returned
   its data as if it were the answer. Fixed by dropping the positional
   fallback for identity-anchored locators: a nonexistent identity now
   fails cleanly through the three-bucket triage instead of returning
   plausible wrong data.
3. **First column unreadable.** A table's leftmost column has no
   previous-sibling cell to serve as its label, so it had no readable ref
   at all and the agent read the wrong cell. Fixed by falling back to the
   column's `<th>` header text — with header-labeled cells getting a
   purely positional locator, since a header identifies a column, never a
   row.
4. **Ambiguous repeated controls.** A "View"-style link repeated once per
   row has the same accessible name in every row; its locator silently
   resolved to a fixed row, opening a *different account's* page than
   requested. Fixed by extending identity re-anchoring to controls inside
   table rows, scoping them to the row containing the declared input's
   value.

Known remaining limitation: `mock_transaction_history`'s
description/amount reads can misread when a transaction description
repeats within one account's history — transaction rows genuinely have
no per-row identity to anchor on.

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

Run the customer site + capability API + employee console (for mock_app
members, also start `python -m mock_app.app` in another terminal first):

```bash
python -m meridian_service.app
```

Then open **http://127.0.0.1:5077/customer** — a public landing page with a
login. Log in with a member number and the demo password `password` (the
same fixed-demo-password convention as the operator/supervisor logins
below). Two kinds of members work:

- **MERIDIAN CORE members** (live external target): `100234`, `100987`,
  `101555`, `102777`, `103001`
- **mock_app members** (your own local target, faster and reset-free):
  `20001`, `20002`, `20003`

That lands on a per-member home page with live account cards and a floating
assistant widget. Once logged in, `member_id` is bound to that session
server-side and stripped from the assistant's tool schema entirely — it is
never accepted from chat text, so a member's chat can only ever act on their
own account, no matter what they type. The tool catalog itself is also
scoped per backend: a mock_app member's chat never even sees MERIDIAN CORE
capabilities, and vice versa. Open
**http://127.0.0.1:5077/** for the employee operations console — viewable
read-only by anyone, but approving, denying, resuming, or aborting a paused
run requires an employee login (`teller1` or `super1`, demo password
`password` — the same identities the replay engine itself signs into
MERIDIAN CORE with). The customer request routes to an approved capability
and then runs the real deterministic replay; interventions, run history, and
a per-step visual trail (screenshots of every step, ending at wherever it
stopped) are visible to the employee. `GET /api/capabilities` and
`POST /api/capabilities/<id>/invoke` are the callable API a calling agent
would use directly, with no knowledge of the underlying UI (each listed
capability carries its `system`). Mutating calls
require an `Idempotency-Key` header and pause at the actual commit action unless
an authorized caller supplies confirmation.

Set `PUBLIC_DEMO_READ_ONLY=true` for every public submission deployment. It
blocks all write capabilities and employee commands until bank SSO/JWT and
role-based authorization are integrated. Set `PUBLIC_DEMO_SYNTHETIC=true` to
run the bundled synthetic bank and safe deterministic read-only chat router;
this public path needs neither private bank credentials nor an LLM API key.
Never deploy production bank credentials with this unauthenticated submission
surface.

Chat routing is hybrid: when `ANTHROPIC_API_KEY` is configured, one LLM tool
call ranks the approved capability artifacts and selects a clear match; when
the key or provider is unavailable, a local artifact relevance ranker takes
over. Both paths ask a specific clarification question rather than guessing
when the intent or a required account ID is unclear.

## Live Demo Path (MERIDIAN CORE)

This is the script for presenting the MERIDIAN CORE stretch adaptation live, in order: breadth first, then the safety/escalation story, then failure-handling.

**Before presenting**, start the service and confirm it's up:

```bash
cd cua-take-home
source .venv/bin/activate
python -m meridian_service.app
```

Open **http://127.0.0.1:5077/customer** for the customer site and
**http://127.0.0.1:5077/** for the employee console. Confirm the console
loads the capability catalog (17 total: 7 MERIDIAN CORE + 10 mock_app;
this demo path uses the MERIDIAN CORE ones).

**One thing to know going in:** this demo environment's data can reset between sessions (member share IDs and balances can change). The numbers below are what worked as of the last test. If the live page shows something different, that's fine — just read out whatever it actually shows. The 5 real members on the live target are `100234`, `100987`, `101555`, `102777`, `103001`, all with demo password `password`. To approve/deny anything from the console, also sign in there as an employee (`teller1`/`password`) — the console is viewable read-only without it.

### Step 1 — Capability catalog (10 seconds)

Point at the dashboard's catalog panel — capabilities are shown as employee-facing cards (friendly name, category, read-only/write, risk, approval requirement), with the raw capability ID and signature tucked behind a "Technical details" disclosure. All 7 required functions are recorded and callable right now. Example signature:

```
meridian_member_balance(member_id, share_id)
  -> {share_balance, share_status}
```

### Step 2 — Customer login and self-service (success path)

At `/customer`, log in as member `100987`. The home page shows two real,
live-fetched account cards — reading every one of a member's shares would
mean a full fresh sign-on per share, so the page shows a couple of real
accounts rather than an exhaustive dynamic listing. Click the floating
assistant icon and type:

```
check the balance of share 100987-MMKT-7
```

Note there is no "for member ..." in the message — `member_id` is bound to
the logged-in session server-side, not accepted from chat text at all. One
The configured LLM call (or artifact-ranked fallback) routes the plain-language
request to a capability name and typed arguments — it never touches the browser. Everything after that is
deterministic replay of a recorded artifact, no model in the loop.
`member_balance` reads the specific share named by `share_id`, not just
whichever share happens to be listed first — verified live across members
with 12+ shares, and a nonexistent or another member's share_id now fails
cleanly instead of silently returning a different share's data (an actual
bug found and fixed while re-recording this capability).

Expected reply (as of the last test — numbers may differ if the environment reset):

```
bot: Done. meridian_member_balance succeeded:
share_balance=$40.00, share_status=OPEN
```

**The session-scoping is worth demonstrating directly:** still logged in as
`100987`, ask the assistant about a share that belongs to a different member,
e.g. `check the balance of share 100234-S0001-6`. It fails cleanly (that
share genuinely doesn't exist on member 100987's page) rather than returning
member 100234's data — the chat literally cannot act on another member's
account, by construction, not just by prompt instruction.

### Step 3 — Chat: funds transfer (escalation path)

Log in as member `102777` and, in the assistant, type:

```
transfer $1 from share 102777-MMKT-3 to share 102777-MMKT-4, memo routine transfer
```

This is a risky, irreversible action, so it pauses instead of just running. Expected: the run shows status `awaiting_human`:

```
bot: I started 'meridian_funds_transfer' but it needs a human decision
before continuing: step 's14_click_post_transfer' is classified RISKY...
Open the dashboard to look at the live session and approve, deny, or
act on it.
```

That pause is enforced inside the replay engine itself, not the chatbot or API layer — no code path can skip it.

The capability pauses once, at the actual irreversible **Post Transfer** step.
The employee can approve or deny it directly from the operations console (sign
in first at `/employee/login` as `teller1`/`password`). The same action is
also available through the command API — it requires that same employee
session (an anonymous request now gets a clean 401, not a silent approval):

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:5077/employee/login \
  -d "employee_id=teller1&password=password"
curl -s -b cookies.txt -X POST http://127.0.0.1:5077/api/runs/RUN_ID/command \
  -H "Content-Type: application/json" -d '{"command":"approve"}'
```

To check the run's current status (same RUN_ID), use:

```bash
curl -s http://127.0.0.1:5077/api/runs/RUN_ID
```

Approve once, then check the run again. If the source share is on `HOLD`, the
engine reports the declared `share_on_hold` business outcome instead of
misclassifying it as an automation failure. If the demo data has changed, use
two shares currently shown as `OPEN` for the member.

### Step 4 — The supervisor-override capability

`place_account_hold` is customer-callable in this demo build the same way the
other six are — log in as member `103001` and, in the assistant, type
`place a hold on share 103001-MMKT-4, reason routine review` to exercise it
(the supervisor credential is infrastructure-bound regardless of which
customer is logged in, same as the operator credential is for the others).
**Worth calling out as a real product question, not glossed over:** a
customer requesting a hold on their *own* account is an unusual self-service
action — placing an account hold (fraud/legal/deceased) is normally a
bank-initiated, employee-only action. A real deployment would likely restrict
this capability to the employee console rather than the customer chat; this
demo exposes all 7 recorded capabilities identically to keep the coverage
story simple. Real recorded outcome from initial adaptation: share
`103001-MMKT-2`, confirmation reference `CN480159`.

### Step 5 — Quick coverage flex (only if time allows)

Log in as member `100234` and ask the assistant `what's my name on file`.

Real recorded result: `member_name=Lovelace, Ada`. `member_inquiry` returns only the member's name — this legacy system has no member-level status field, only per-share status (that's what `member_balance` is for); an earlier version of this capability approximated a "member status" from whichever share happened to be listed first, which silently broke once that member's first share stopped being the OPEN one. This is the 7th function, read-only member lookup. Skip this step if short on time — Steps 1–4 already prove the important things.

### Step 6 — An exceptional state (proves the 3-bucket taxonomy)

Customer chat can no longer reference an arbitrary member number (Step 2's
scoping demo is why), so this one goes through the direct capability API
instead — representing a calling agent invoking a capability directly, the
same surface `GET /api/capabilities` documents:

```bash
curl -s -X POST http://127.0.0.1:5077/api/capabilities/meridian_member_inquiry/invoke \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-not-found-1" \
  -d '{"params":{"member_id":"999999"}}'
```

Poll `GET /api/runs/RUN_ID` for the result. Expected: a clean `member_not_found` business outcome (HTTP 404 under the hood) — reported as a normal, non-alarming result, not an error. Every run in this system sorts into exactly one of three buckets: business outcome, recoverable condition, hard failure — including runs coming through the customer chat and this API layer, verified live.
