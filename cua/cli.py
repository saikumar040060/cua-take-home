"""Command-line entry points.

  python -m cua discover --spec specs/open_sub_account.json --out artifacts/open_sub_account.json
  python -m cua replay artifacts/open_sub_account.json --param member_id=10023 ... [--confirm-risky] [--escalate]
  python -m cua catalog

The mock target app runs separately:  python -m mock_app.app
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.browser import BrowserSession
from cua.discovery.agent import DiscoveryAgent
from cua.discovery.spec import GoalSpec
from cua.replay.engine import ReplayEngine
from cua.runlog import RunLogger, new_run_id
from cua.safety.policy import DEFAULT_POLICY_PATH, Policy, PolicyGate
from cua.safety.redact import Redactor
from cua.schema import Capability, Sensitivity


def _redactor_for_spec(spec: GoalSpec) -> Redactor:
    redactor = Redactor()
    for p in spec.inputs:
        if p.sensitivity == Sensitivity.IDENTIFIER:
            redactor.register_identifier(p.value)
        elif p.sensitivity == Sensitivity.SECRET:
            redactor.register_secret(p.value)
    return redactor


def cmd_discover(args: argparse.Namespace) -> int:
    spec = GoalSpec.load(args.spec)
    policy = Policy.load(args.policy)
    gate = PolicyGate(policy)
    redactor = _redactor_for_spec(spec)
    run_id = new_run_id("discovery")
    logger = RunLogger(args.runs_dir, run_id, redactor)

    from cua.discovery.decider import AnthropicDecider

    decider = AnthropicDecider(model=args.model)

    print(f"[discover] run {run_id} — goal: {spec.goal}")
    with BrowserSession(headed=args.headed) as session:
        agent = DiscoveryAgent(
            session, spec, decider, gate, logger,
            approve_risky=args.approve_risky,
        )
        outcome = agent.run()

        if not outcome.success or outcome.capability is None:
            print(f"[discover] FAILED after {outcome.turns} turns: {outcome.summary}")
            print(f"[discover] evidence: {logger.dir}")
            return 1

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(outcome.capability.model_dump_json(indent=2))
        logger.event("artifact_saved", actor="automation", path=str(out_path))
        # Keep a copy of the artifact beside the run evidence.
        (logger.dir / "artifact.json").write_text(
            outcome.capability.model_dump_json(indent=2)
        )

    print(f"[discover] SUCCESS in {outcome.turns} turns: {outcome.summary}")
    print(f"[discover] outputs: {redactor.scrub_obj(dict(outcome.outputs))}")
    print(f"[discover] artifact: {out_path}")
    print(f"[discover] evidence: {logger.dir}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    capability = Capability.model_validate_json(Path(args.artifact).read_text())
    policy = Policy.load(args.policy)
    gate = PolicyGate(policy)

    params: dict[str, str] = {}
    for pair in args.param or []:
        name, _, value = pair.partition("=")
        if not _:
            print(f"bad --param {pair!r}; expected name=value", file=sys.stderr)
            return 2
        params[name] = value

    redactor = Redactor()
    run_id = new_run_id("replay")
    logger = RunLogger(args.runs_dir, run_id, redactor)

    print(f"[replay] run {run_id} — {capability.signature()}")
    with BrowserSession(headed=args.headed) as session:
        engine = ReplayEngine(
            session, capability, gate, logger,
            confirm_risky=args.confirm_risky,
            escalate_on_failure=args.escalate,
        )
        result = engine.run(params)

    print(f"[replay] status: {result.status.value}")
    if result.business_outcome:
        print(f"[replay] business outcome: {result.business_outcome.code} — "
              f"{result.business_outcome.description}")
    if result.failure:
        print(f"[replay] failed at {result.failure.step_id}: "
              f"expected {result.failure.expected!r}; "
              f"observed {result.failure.observed!r}")
        print(f"[replay] debug: {result.failure.screenshot_path} "
              f"{result.failure.dom_snapshot_path}")
    if result.outputs:
        print(f"[replay] outputs: {json.dumps(result.outputs)}")
    for rec in result.recoveries:
        print(f"[replay] recovered: {rec.rule_id} at {rec.at_step_id} "
              f"(#{rec.application_count})")
    print(f"[replay] evidence: {logger.dir}")
    return 0 if result.status.value != "hard_failure" else 1


def cmd_catalog(args: argparse.Namespace) -> int:
    """List saved artifacts the way a calling agent would see them."""
    root = Path(args.artifacts_dir)
    for path in sorted(root.glob("*.json")):
        cap = Capability.model_validate_json(path.read_text())
        print(f"{cap.signature()}   [v{cap.version}, schema {cap.schema_version}]")
        print(f"    {path}")
        first_line = cap.description.strip().splitlines()[0]
        print(f"    {first_line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="LLM-driven discovery run")
    p_disc.add_argument("--spec", required=True, help="GoalSpec JSON path")
    p_disc.add_argument("--out", required=True, help="artifact output path")
    p_disc.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    p_disc.add_argument("--runs-dir", default="runs")
    p_disc.add_argument("--model", default=None)
    p_disc.add_argument("--headed", action="store_true")
    p_disc.add_argument(
        "--approve-risky", action="store_true",
        help="operator pre-approval for risky actions in this run (logged)",
    )
    p_disc.set_defaults(func=cmd_discover)

    p_rep = sub.add_parser("replay", help="deterministic replay (no LLM)")
    p_rep.add_argument("artifact", help="capability artifact JSON path")
    p_rep.add_argument("--param", action="append", metavar="NAME=VALUE")
    p_rep.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    p_rep.add_argument("--runs-dir", default="runs")
    p_rep.add_argument("--headed", action="store_true")
    p_rep.add_argument(
        "--confirm-risky", action="store_true",
        help="caller sign-off: risky steps may execute in this invocation",
    )
    p_rep.add_argument(
        "--escalate", action="store_true",
        help="on hard failure, hand the live session to the operator console",
    )
    p_rep.set_defaults(func=cmd_replay)

    p_cat = sub.add_parser("catalog", help="list saved capabilities")
    p_cat.add_argument("--artifacts-dir", default="artifacts")
    p_cat.set_defaults(func=cmd_catalog)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
