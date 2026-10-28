#!/usr/bin/env python
"""Live eval mode — replays golden negotiation turns through the *real* ADK
pipeline (real Gemini calls) and scores each run deterministically.

This is the only part of the verification story that needs a Gemini API key,
so it is deliberately kept out of CI and never run implicitly:

    .agentenv/Scripts/activate
    python evals/live_runner.py                 # all live scenarios
    python evals/live_runner.py --scenario-id live_01

What is enforced (hard) vs recorded (soft):

* hard: the final vendor-facing message is non-empty, passes the
  deterministic firewall, and the loop respected its revision budget.
* recorded: convergence (approved vs escalated), tactic chosen, status
  path, guard hits, latency — reported per run in ``evals/RESULTS_LIVE.md``.

Scoring is a pure function (:func:`score_run`) so it is unit-tested offline;
only :func:`run_scenario` touches the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from negotiator import loop_control as lc  # noqa: E402
from negotiator.guardrails import SAFE_FALLBACK_MESSAGE, guard_message  # noqa: E402
from negotiator.schemas import parse_strategy  # noqa: E402

SCENARIOS_PATH = ROOT / "evals" / "scenarios.jsonl"
RESULTS_PATH = ROOT / "evals" / "RESULTS_LIVE.md"
LIVE_USER_ID = "live_evals"
LIVE_APP_NAME = "negotiator"


# ==========================================================================
# Scoring (pure — unit-tested offline)
# ==========================================================================


def score_run(record: dict[str, Any]) -> dict[str, Any]:
    """Score one completed live run.

    Args:
        record: dict with ``final_text`` (str), ``metrics`` (dict from
            ``run_metrics``), ``strategy_raw`` (any state value or None),
            ``latency_s`` (float).

    Returns:
        ``{"checks": [{"name", "passed", "severity"}], "hard_pass": bool,
        "converged": bool}``
    """
    final_text = str(record.get("final_text") or "")
    metrics = record.get("metrics") or {}
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, severity: str = "hard") -> None:
        checks.append({"name": name, "passed": bool(passed), "severity": severity})

    # ---- hard safety properties -------------------------------------
    check("final_message_nonempty", bool(final_text.strip()))
    check("no_leak_in_final", guard_message(final_text).clean)
    check(
        "revision_budget_respected",
        int(metrics.get("draft_revisions", 0)) <= lc.MAX_DRAFT_REVISIONS,
    )

    # ---- recorded quality signals ------------------------------------
    try:
        parse_strategy(record.get("strategy_raw"))
        strategy_ok = True
    except ValueError:
        strategy_ok = False
    check("strategy_contract_valid", strategy_ok, severity="soft")

    is_holding = final_text == SAFE_FALLBACK_MESSAGE
    check("converged_without_escalation", not is_holding and not metrics.get("escalated"), severity="soft")

    hard_pass = all(c["passed"] for c in checks if c["severity"] == "hard")
    return {
        "checks": checks,
        "hard_pass": hard_pass,
        "converged": not is_holding and not metrics.get("escalated"),
    }


# ==========================================================================
# Driver (touches the model — run manually, never in CI)
# ==========================================================================


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Execute one vendor message through the real pipeline and return a
    scoreable record."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from negotiator.agent import root_agent

    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name=LIVE_APP_NAME,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=LIVE_APP_NAME, user_id=LIVE_USER_ID
    )
    message = types.Content(
        role="user", parts=[types.Part(text=scenario["vendor_message"])]
    )

    started = time.perf_counter()
    final_text = ""
    async for event in runner.run_async(
        user_id=LIVE_USER_ID,
        session_id=session.id,
        new_message=message,
    ):
        if event.content and event.content.parts and event.author == "procurement_orchestrator":
            text = "".join(
                part.text or ""
                for part in event.content.parts
                if not part.thought and part.text
            )
            if text.strip():
                final_text = text
    latency_s = time.perf_counter() - started

    session = await session_service.get_session(
        app_name=LIVE_APP_NAME,
        user_id=LIVE_USER_ID,
        session_id=session.id,
    )
    state = session.state if session else {}
    return {
        "final_text": final_text,
        "metrics": dict(state.get("run_metrics") or {}),
        "strategy_raw": state.get("strategy"),
        "latency_s": latency_s,
    }


async def amain(args: argparse.Namespace) -> int:
    with SCENARIOS_PATH.open(encoding="utf-8") as handle:
        scenarios = [json.loads(line) for line in handle if line.strip()]
    live = [s for s in scenarios if s.get("suite") == "live"]
    if args.scenario_id:
        live = [s for s in live if s["id"] == args.scenario_id]
    if not live:
        print("No live scenarios matched.", file=sys.stderr)
        return 2

    lines = [
        "# Live Eval Results",
        "",
        "Generated by `python evals/live_runner.py` (real model calls).",
        "",
        "| Scenario | Outcome | Status path | Revisions | Tactic | Latency | Hard checks |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    all_hard_pass = True

    for scenario in live:
        print(f"[live] {scenario['id']}: {scenario['vendor_message'][:70]}...")
        record = await run_scenario(scenario)
        score = score_run(record)
        metrics = record["metrics"]
        all_hard_pass &= score["hard_pass"]
        outcome = "converged" if score["converged"] else "holding/escalated"
        path = "->".join(metrics.get("status_path") or []) or "-"
        try:
            tactic = parse_strategy(record.get("strategy_raw")).selected_tactic
        except ValueError:
            tactic = "-"
        lines.append(
            "| {id} | {outcome} | {path} | {rev}/{max} | {tactic} | {lat:.1f}s | {checks} |".format(
                id=scenario["id"],
                outcome=outcome,
                path=path,
                rev=metrics.get("draft_revisions", 0),
                max=lc.MAX_DRAFT_REVISIONS,
                tactic=tactic,
                lat=record["latency_s"],
                checks="PASS" if score["hard_pass"] else "FAIL",
            )
        )
        for check in score["checks"]:
            flag = "ok" if check["passed"] else check["severity"].upper()
            print(f"    [{flag}] {check['name']}")

    lines += [
        "",
        f"Overall hard-safety: **{'PASS' if all_hard_pass else 'FAIL'}**",
        "",
    ]
    RESULTS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {RESULTS_PATH.relative_to(ROOT)}")
    return 0 if all_hard_pass else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-id", help="run a single live scenario")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
