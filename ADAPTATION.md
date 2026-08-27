# Adaptation write-up — MERIDIAN CORE

## What adapting the core actually required

The core needed almost no changes to point at a completely unfamiliar target. Discovery, the artifact schema, the replay engine, the policy gate, and redaction are all unmodified in their logic — only configuration (a new `policy_meridian.json` allowlisting the new origin) and new goal specs (`specs/meridian_core/*.json`, one per function) were needed to run against MERIDIAN CORE at all.

One genuine code change was required, and it is small and additive: `StateProbe` gained an optional `http_status` field (`cua/schema.py`), and `BrowserSession` now tracks the last main-frame navigation's HTTP status on the page object so `probe_holds` can check it (`cua/browser.py`). This exists because MERIDIAN CORE signals several exceptional states via status code (400/403/404/440/503) rather than distinguishing them purely through copy, which the original mock app never did. `GoalSpec`'s `SpecBusinessOutcome`/`SpecInterstitial` gained a matching optional field so a spec author can declare it, and the recorder passes it through unchanged otherwise. All 41 original tests pass unmodified after this change — it is additive, not a rewrite, and every other target keeps working exactly as before.

Recording the actual capabilities surfaced three categories of friction, all resolved at the goal-spec level rather than in code, which is the adaptation boundary working as intended:

* **Perception granularity.** The page-reading tool exposes individual interactive elements and label-adjacent data cells, not whole table containers. An early goal for "check a member's balance" asked the agent to read an entire shares table in one step; the agent picked a plausible-looking but wrong target (a column header) instead of failing loudly. The fix was scoping the goal to what a single read step can actually do — read one specific share's balance and status — which is also a more honest capability boundary than "dump everything visible."
* **Guessing under ambiguity.** Several goal specs initially declared example values (a product name, a share ID, a share type) that didn't exist on the real target. In each case the agent correctly refused to guess and paused for a human rather than picking something plausible — exactly the safety behavior the brief asks for, just triggered by my own spec errors rather than the target's design. Each was fixed by supplying real values read off the live page (via the operator console's `look`) and re-running discovery cleanly from scratch, since steps performed manually during a paused handoff are not serialized into the artifact (a known limitation, see What's next). Once this pattern was recognized, later specs (`open_new_share`, `place_account_hold`) were written up front to have the agent read the real option off the live dropdown itself rather than trusting a literal example value, avoiding the failure entirely.
* **Text with no element reference.** `update_member_info`'s save confirmation ("CHANGES SAVED") is plain page text with no backing element reference, so the read tool — which resolves a target by reference, the same mechanism behind every click and fill — had no way to capture it, and the agent correctly said so rather than guessing at a nearby link. This is a real, narrower boundary than the table-reading one above: not every visible string is a valid read target. The fix was to verify the mutation the same way `member_balance` reads data — after saving, navigate back to the member's record and read the now-updated email address as a labeled field, which both sidesteps the unreadable banner and is arguably stronger evidence, since it confirms the write actually took effect rather than just that a message appeared.

## The capability API contract

`meridian_service/app.py` exposes the recorded capabilities as plain HTTP:

* `GET /api/capabilities` — the catalog: each capability's `capability_id`, human-readable `signature()` (e.g. `meridian_member_balance(operator_id, password, member_id) -> {first_share_balance, first_share_status}`), typed inputs/outputs, and declared business outcomes. A caller (or the chatbot) can build a valid invocation from this alone, with no knowledge of the underlying UI.
* `POST /api/capabilities/<id>/invoke` — runs the real `ReplayEngine` in a background thread (Playwright's sync API requires one dedicated thread per session, the same pattern used elsewhere for this project) and returns a `run_id` immediately; the caller polls `GET /api/runs/<run_id>` for status (`running` / `awaiting_human` / `done` / `error`) and, on completion, the same structured result shape `ReplayResult` already produces (`status`, `outputs`, `business_outcome`, `failure`, `recoveries`).
* `POST /api/runs/<run_id>/command` — feeds a command (`look`, `approve`, `deny`, `click <ref>`, ...) into the paused run's live `OperatorConsole`, exactly like the terminal console, just over HTTP.

The API never runs discovery and never calls an LLM to decide a UI action — only `ReplayEngine` executes, so every guarantee replay already had (determinism, the three-bucket outcome contract, the policy gate) applies unchanged to every API call.

## Driving the legacy UI reliably, and detecting its exceptional states

Nothing new was needed to walk the review→post confirmation flow or read the hidden per-transaction token — the agent handles both as ordinary steps within its existing observe → decide → act → verify loop, and the recorded artifact's steps and checkpoints capture them like any other step. The real funds-transfer capability reads the token and posts through review exactly as recorded, producing a genuine confirmation reference on replay.

Exceptional states are still classified into the same three buckets, now checked via text *and* HTTP status where the target signals a state that way: a missing member (`404`), a share on `HOLD` blocking a transfer, and a rejected form are declared as **business outcomes** (a legitimate "no," not a failure); a session or maintenance interstitial is declared as a **recovery rule** (bounded, auto-dismissed); anything undeclared still falls through to a **hard failure** with the same step/expected/observed/screenshot/DOM debug bundle as always. No fourth category was introduced anywhere.

## How safety, evidence, and escalation survived the new layers

All three were verified live through the new surface, not just asserted:

* **Safety** — the same `PolicyGate` (now loaded from `policy_meridian.json`) checks every action regardless of whether it originated from the CLI, the API, or the chatbot; a real chat request to run the funds transfer was correctly paused rather than executed, because the risky-action rule is enforced inside `ReplayEngine` itself, not in any caller.
* **Evidence** — `RunLogger` and `Redactor` are unmodified; a real API-triggered run's `events.jsonl` and `result.json` on disk show the member number masked (`1****7`), never raw, identical to CLI-driven runs.
* **Escalation** — the same gap fixed previously in a separate dashboard project reappears here in principle and is fixed the same way: `TrackedConsole` scrubs the in-memory `InterventionRequest` through the run's `Redactor` before it's exposed via `GET /api/runs/<id>`, so a paused run's intervention detail is redacted the same as the persisted copy.

## What's next

All seven required functions are recorded and verified against the live target: sign-on, member balance, funds transfer, member inquiry, opening a new share, updating member contact information, and placing a supervisor-gated account hold. The two explicit must-haves (balance check, funds transfer) are covered, and so is every distinct risk/permission pattern the brief described — a plain read, a teller-level irreversible action, a form submission with validation, and a supervisor-gated irreversible override.

`place_account_hold`'s spec deliberately avoids a hardcoded share ID or reason-code string — both known failure modes from the funds-transfer recording — and instead has the agent confirm the chosen member has no share already on HOLD, pick the first OPEN share, and select the closest-matching real reason code off the live dropdown; it recorded successfully on the first attempt written this way. Two risky steps were captured for it: the navigation click into the hold screen and the final posting click, the same over-broad risky-pattern match noted below. `update_member_info` took three attempts (see "text with no element reference" above) before landing on reading the mutated field back rather than the unreadable confirmation banner.

The chatbot and dashboard are intentionally minimal — no auth, no styling system, no run-history pagination — exactly as invited ("keep both intentionally simple").

Next, in order: fold manually-performed handoff steps back into the artifact automatically instead of requiring a clean re-record; and tighten the risky-action name pattern, which currently classifies the *navigation link* into a risky action (e.g. "Funds Transfer", "Place Account Hold") as risky itself (matching the action name in its accessible name) rather than only the actual posting action — safe-by-default, but worth narrowing so escalation happens at the step that actually mutates state rather than one step earlier than necessary.
