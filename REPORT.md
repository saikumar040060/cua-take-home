# Design write-up

## Architecture

The system is a single Python process per run with four cleanly separated
parts: a **discovery agent** (LLM in the loop), a **replay engine** (no LLM),
a **safety layer** both of them execute through, and an **escalation seam**
both of them can hand a live session to. The connecting tissue is the
**capability artifact**: discovery's only job is to produce it; replay's only
job is to consume it. The two sides never share execution code paths beyond
the browser/locator utilities and the policy gate — which is deliberate,
because the discovery loop is allowed to be exploratory while replay must be
boring and predictable.

Perception is a structured, semantic page snapshot (roles, accessible names,
visible text, labeled data cells) rather than screenshots. This is the
representation that still exists when the DOM is hostile — and it is the same
shape an OS accessibility API yields for a desktop app, which keeps the
observe/act seam surface-agnostic (see Heterogeneity). Screenshots are
captured as evidence, not perception.

I kept it single-process and synchronous on purpose: the brief rewards
judgment, not infrastructure. The seams where queues/services would attach in
production are the artifact store (files today), the intervention channel
(stdin console today), and the run log (JSONL today).

## Artifact schema

The artifact (`cua/schema.py`) is a versioned Pydantic model designed to be
read three ways: by the replay engine (execution), by a calling agent (a
function signature: `open_sub_account(member_id, ...) -> {confirmation_ref}`
plus declared business outcomes), and by a human reviewer (every target and
checkpoint carries a prose description; provenance records the goal, model,
and run that produced it).

Three decisions shaped it. First, **element identity is an ordered fallback
chain**, semantic-first: role + accessible name, then attribute CSS (a form
field's `name=` is load-bearing for the server, hence stable), then visible
text, then a structural DOM path as last resort. The chain encodes a
robustness argument, and resolution below the primary is surfaced as a
"degraded locator" drift signal rather than a silent pass. A subtlety worth
noting: the snapshot's heuristic labels (text in the adjacent table cell —
common in legacy layouts) are *not* real ARIA accessible names, so the
recorder refuses to emit role-based locators from them; those fields lead
with the attribute selector instead. Data cells get a label-relative locator
("the cell next to 'Confirmation Ref'") because their own text is the value,
which varies per run.

Second, **values are templates, never literals**. The recorder replaces
concrete run values with `{param}` references and generalizes URLs into
patterns. This is what turns a recorded macro into a parameterized
capability, and it doubles as a privacy property: run-time identifiers never
persist inside artifacts (enforced by test).

Third, the **error taxonomy is part of the contract, not the code**. Business
outcomes ("member not found", "validation rejected") and known interstitials
are declared in the goal spec by whoever commissions the capability, and are
recorded into the artifact as detectors and recovery rules. A successful
discovery run never witnesses "member not found" — treating it as contract
rather than observation is the only honest way replay can recognize it later.
The LLM decides *how* to do the flow; it does not define *what* the
capability's interface is.

## Determinism & error handling

Replay never asks a model anything. Per step: resolve through the fallback
chain (ambiguity — more than one match — skips to the next strategy rather
than guessing), gate the action, act, then verify a **checkpoint** with a
condition-based wait (poll-until-true within the step's budget; there are no
blind sleeps — transient slowness is absorbed here, demonstrated by the
`MOCK_CHAOS=slow` run). Checkpoints come from the discovery run itself: the
model must state, per action, the stable UI text it expects as proof of
success; the executor verifies it live, and the recorder strips any
expectation that embeds a run-specific value.

When a step cannot proceed, the engine classifies the live state in strict
order: (a) **declared business outcomes** — terminal, returned with their
code; checked first because a legitimate "no" must never read as breakage;
(b) **recovery rules** — known interruptions with a recorded deterministic
fix (dismiss the session interstitial), bounded by a per-rule application
budget so recovery can never loop, then the checkpoint is re-verified;
(c) **hard failure** — stop and return step, expected, observed, plus a
screenshot and DOM snapshot. The result object (`cua/replay/result.py`)
makes the three buckets explicit, carries typed outputs on success, and
lists every recovery applied. UI drift is secondary by design (stable
enterprise UIs), but the degraded-locator signal gives early warning without
failing runs.

## Heterogeneity & multi-tenant

**Surface abstraction.** The recorded flow speaks in surface-neutral terms:
targets are semantic descriptions with ordered locator strategies, probes are
"URL/window state + visible text + element present", actions are
click/fill/select/read. What varies by surface is the *perceiver/actor pair*:
for web it is Playwright + DOM/ARIA; for a desktop app the same `Snapshot`
(role, name, text, options) is derivable from UIA/AX accessibility APIs, with
`dom_path` becoming an accessibility-tree path and `url_pattern` becoming a
window/screen identifier probe. The seam is exactly the `take_snapshot` /
`resolve_target` / `probe_holds` trio in `cua/browser.py`; the schema,
recorder logic, replay classification, policy gate, and escalation model
carry over unchanged. A screenshot+coordinates driver could also implement
the same seam for surfaces with no accessibility tree at all, at the cost of
weaker checkpoints.

**Multi-tenant reuse.** Many tenants run the same vendor app, configured and
branded differently. The artifact already separates the durable parts
(semantic locators, step order, outcome detectors) from the tenant-specific
parts (origin, entry URL, copy variations). I would ship a capability as a
**base artifact keyed by (vendor app, version) plus a per-tenant binding**:
origin/entry mapping, and a sparse overlay of locator or checkpoint
overrides recorded only where the base fails. The degraded-locator and
recovery telemetry is the drift detector: a tenant whose runs resolve via
fallbacks or fail checkpoints gets flagged for a re-validation run against
that tenant, which either updates its overlay or bumps the base version.
Artifact `schema_version` handles format migrations; capability `version`
plus provenance handles flow re-records. None of this is implemented — but
the schema was shaped so it bolts on without redesign (nothing tenant-bound
lives inside steps except through templates and origin-relative URLs).

## Escalation & handoff

"Stuck" is detected at four points: the model declares `blocked`, discovery
exhausts its step budget, replay hits a hard failure (with `--escalate`), or
a risky step lacks sign-off. All four raise a structured
`InterventionRequest` — capability/goal, current step, why it stopped, the
current URL, a screenshot, and the tail of the event log — persisted to the
run directory.

The handoff itself is the part the brief calls the seam, and it is real: the
automation pauses and an **operator console takes over the same live
Playwright page** — not a fresh session, so cookies, server session state,
and half-completed forms are preserved. Control is single-holder and
explicit: while the human holds it, automation issues nothing; every human
command is policy-checked by the *same* gate (an operator cannot take the
session off-allowlist) and recorded into the intervention record and the
JSONL log with `actor="human"`. `resume`/`approve`/`deny`/`abort` hand
control back; replay then re-verifies the current step's checkpoint before
continuing, and discovery re-observes so the model sees what the human
changed. The console reads commands from a stream (stdin interactively, a
pipe in tests/evidence), which is exactly where a real operator UI would
attach; building that UI is out of scope per the brief, the control-transfer
model is not.

## Safety

Everything funnels through one `PolicyGate` choke point checked before every
action — discovery, replay, recovery actions, and human console commands
alike. The policy is data (`policy.json`): allowed origins, allowed/blocked
path prefixes, allowed action types. Violations are loud failures, never
retries.

Risky actions are classified by pattern over the control's accessible name
(create/submit/delete/transfer/...) and stamped into the artifact per step,
so a reviewer can see which steps are destructive. They never run
unattended: discovery requires an explicit, logged `--approve-risky`
operator flag or a live confirmation; replay requires the caller's
per-invocation `confirm_risky` sign-off, otherwise it escalates to a human
instead of submitting.

Data handling: artifacts contain templates, not values, so PII never
persists there. Logs and DOM snapshots pass through a redactor at the single
write boundary — declared identifier params are masked, declared secrets are
erased and never persisted anywhere, and pattern scrubbing (SSN, account
numbers, phone, card-like digit runs) applies regardless of declarations.
Replay still *returns* real outputs to the caller (it must — that is the
point), with output sensitivity declared in the schema so a production
caller knows what it is holding. Screenshots are stored unredacted — with
synthetic data here; production would need visual masking (see Cuts).

## Cuts

Deliberately not built: any scaling infrastructure (queues, workers,
multi-tenant plumbing); a real operator UI (console only — the transfer
mechanism is the point); desktop/screenshot surface drivers (designed for at
the snapshot/resolve/probe seam, not implemented); artifact
canonicalization across tenant variants (designed above); screenshot
redaction; artifact approval workflow (draft → reviewed → approved for
unattended replay) — today `--confirm-risky` stands in for the caller-side
half of that.

Known limitations I would fix next, in order: (1) steps a human performs
during a discovery handoff are recorded as evidence but not serialized into
the artifact, so such runs need a follow-up re-record — the fix is
translating console commands into recorded steps through the same recorder;
(2) a bounded, policy-checked single-step LLM "assisted fallback" on replay
locator failure, recorded as evidence and gated exactly like discovery;
(3) multi-run stability scoring (replay N times, report flakiness) feeding
the approval state; (4) richer checkpoint predicates (negative assertions,
extracted-value validation against the declared output type).
