# Evidence

All runs below are real: a live Flask instance of `mock_app`, a real headless
Chromium via Playwright, and — for `01_discovery` — a genuine Anthropic API
call per decision (model: `claude-sonnet-4-5-20250929`). No step, action, or
outcome here is scripted or hand-written; these are the actual
`runs/<run_id>/` directories produced by `python -m cua discover` / `replay`,
copied verbatim (same `events.jsonl`, screenshots, DOM snapshots).

| Directory | What it shows |
|---|---|
| `01_discovery` | Real LLM discovery run. Goal: look up a member, open a sub-account, reach confirmation, read the reference. 9 observe→decide→act turns, 8 steps recorded into the artifact. `events.jsonl` has one `decide` event per turn with the model's tool call and reasoning. |
| `artifact_open_sub_account.json` | The capability artifact produced by that run — the one used for every replay below. |
| `02_replay_success` | Deterministic replay, different member/params than discovery (`member_id=10456`, product `Money Market`) — proves the artifact generalizes, not just replays the exact discovery values. Status: `success`. |
| `03_replay_business_outcome_member_not_found` | Replay with an unknown member number. Status: `business_outcome`, code `member_not_found` — a legitimate negative result, not a failure. |
| `04_replay_business_outcome_validation_rejected` | Replay with a deposit below the $5 minimum. Status: `business_outcome`, code `validation_rejected`. |
| `05_replay_recovered_interstitial` | Mock app restarted with `MOCK_CHAOS=interstitial`. The session-expiry interstitial appears after search; the recorded recovery rule dismisses it and the run completes. `result.recoveries` shows the applied rule. |
| `06_replay_hard_failure` | Mock app restarted with `MOCK_CHAOS=broken` (simulated host error page). Replay stops at step `s02_click_search` with a structured `FailureDetail` (step, expected, observed) plus a screenshot and DOM snapshot in `screenshots/` and `dom/`. **This is the required "replay that deliberately hits an exceptional state."** |
| `07_replay_escalation_handoff` | Replay invoked with `--escalate` and `confirm_risky=false`. At the risky "Create Sub-Account" step, automation raises an `InterventionRequest` (see `iv-*.json`), pauses, and hands the **same live session** to the operator console. A human operator (scripted `approve` command, `human_action`/`handoff_started`/`handoff_ended` events in the log) approves the risky step; automation resumes on the same page and completes. |

## How to read a run directory

- `events.jsonl` — structured, redacted event log (one JSON object per
  observe/decide/act/verify/recovery/escalation event).
- `screenshots/`, `dom/` — captured on checkpoint failures and interventions.
- `result.json` (replay) — the full `ReplayResult` (status, outputs,
  business_outcome, failure, recoveries, per-step reports).
- `artifact.json` (discovery) — a copy of the capability produced by that run.
- `iv-*.json` — persisted `InterventionRequest`, including the human's
  recorded actions and resolution (present in `06` if `--escalate` had been
  set there, and in `07`).

## Redaction, verified

Member numbers are declared `sensitivity: identifier` in
`specs/open_sub_account.json`. Across every file in this directory the raw
values (`10023`, `10456`, `10777`, `99999`) never appear — they are masked
(e.g. `10023` → `1***3`) in logs, and never appear at all in the capability
artifact (which stores `{member_id}` templates, not literals).
