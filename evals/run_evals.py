#!/usr/bin/env python
"""Offline eval harness for the negotiation pipeline. Runs with no API key.

Three suites over the labeled golden set in ``evals/scenarios.jsonl``:

1. **Guardrail detection** — does the deterministic firewall block every
   labeled leak (block rate) while letting clean negotiation language
   through (specificity)? Category-level precision/recall over findings.
2. **Contract conformance** — do typed handoffs parse when models behave
   (fenced JSON, prose, coercion) and *fail closed* when they don't?
3. **Loop simulation** — scripted generator-critic runs through the real
   ``loop_control`` state machine: does the loop converge within its
   revision budget, escalate when stuck, survive garbage verdicts, and
   leak zero internal data to the final user-facing message?

Usage:
    .agentenv/Scripts/activate && python evals/run_evals.py

Exit code 0 only when every hard safety threshold holds (100% block rate
on leaks, 0 leak-throughs, 100% fail-closed on broken contracts).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from negotiator import loop_control as lc  # noqa: E402
from negotiator.guardrails import (  # noqa: E402
    SAFE_FALLBACK_MESSAGE,
    guard_message,
)
from negotiator.schemas import (  # noqa: E402
    CONTRACT_VIOLATION_MARKERS,
    parse_strategy,
    parse_verdict_failsafe,
)

SCENARIOS_PATH = ROOT / "evals" / "scenarios.jsonl"
RESULTS_PATH = ROOT / "evals" / "RESULTS.md"


def load_scenarios() -> list[dict]:
    with SCENARIOS_PATH.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ==========================================================================
# Suite 1 — guardrail detection
# ==========================================================================


def run_guard_suite(cases: list[dict]) -> dict:
    leaky_total = leaky_blocked = 0
    clean_total = clean_passed = 0
    tp = fp = fn = 0
    rows: list[tuple[str, bool, bool, str]] = []

    for case in cases:
        report = guard_message(case["text"])
        expected_clean = case["expected_clean"]
        expected_categories = set(case["expected_categories"])
        detected = {f.category for f in report.findings}

        tp += len(expected_categories & detected)
        fp += len(detected - expected_categories)
        fn += len(expected_categories - detected)

        if expected_clean:
            clean_total += 1
            clean_passed += int(report.clean)
        else:
            leaky_total += 1
            leaky_blocked += int(not report.clean)

        matched = (report.clean == expected_clean) and (detected == expected_categories)
        rows.append((case["id"], report.clean, matched, ",".join(sorted(detected)) or "-"))

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "rows": rows,
        "leaky_total": leaky_total,
        "leaky_blocked": leaky_blocked,
        "clean_total": clean_total,
        "clean_passed": clean_passed,
        "block_rate": leaky_blocked / leaky_total if leaky_total else 1.0,
        "specificity": clean_passed / clean_total if clean_total else 1.0,
        "precision": precision,
        "recall": recall,
    }


# ==========================================================================
# Suite 2 — contract conformance (verdict + strategy)
# ==========================================================================


def run_contract_suite(cases: list[dict]) -> dict:
    ok_total = ok_passed = 0
    bad_total = bad_failed_closed = 0
    rows: list[tuple[str, str, str]] = []

    for case in cases:
        raw = case["text"]
        if case["suite"] == "verdict":
            verdict = parse_verdict_failsafe(raw)
            broke = bool(set(verdict.violations) & set(CONTRACT_VIOLATION_MARKERS))
        else:  # strategy
            broke = False
            try:
                directive = parse_strategy(raw)
                if case.get("expect_tactic"):
                    assert directive.selected_tactic == case["expect_tactic"]
            except (ValueError, AssertionError):
                broke = True

        if case["expect"] == "parse_ok":
            ok_total += 1
            ok_passed += int(not broke)
            observed = "parsed" if not broke else "REJECTED"
        else:
            bad_total += 1
            bad_failed_closed += int(broke)
            observed = "failed closed" if broke else "ACCEPTED (!)"

        rows.append((case["id"], case["expect"], observed))

    return {
        "rows": rows,
        "ok_total": ok_total,
        "ok_passed": ok_passed,
        "bad_total": bad_total,
        "bad_failed_closed": bad_failed_closed,
    }


# ==========================================================================
# Suite 3 — loop simulation
# ==========================================================================


def simulate_turn(case: dict) -> dict:
    """Drive one scripted negotiation turn through the real loop machine."""
    state: dict = {}
    lc.begin_turn(state)
    statuses: list[str] = []

    final_text: str | None = None
    for index, draft in enumerate(case["drafts"]):
        lc.record_draft_revision(state)
        state[lc.CURRENT_DRAFT_KEY] = draft
        raw_verdict = case["verdicts"][min(index, len(case["verdicts"]) - 1)]
        verdict, _ = lc.resolve_audit(state, raw_verdict, draft_text=draft)
        statuses.append(verdict.status)
        if verdict.status in ("approved", "escalated"):
            final_text = verdict.final_message or ""
            break

    if final_text is None:  # budget path always escalates; belt and braces
        final_text = SAFE_FALLBACK_MESSAGE

    # The orchestrator's own output goes through the firewall last.
    outgoing = case.get("root_output") or final_text
    override = lc.enforce_output_firewall(state, outgoing)
    if override is not None:
        outgoing = override.parts[0].text

    return {
        "final_text": outgoing,
        "statuses": statuses,
        "metrics": lc.run_metrics(state),
    }


def run_loop_suite(cases: list[dict]) -> dict:
    total = passed = 0
    leak_throughs = 0
    rows: list[tuple[str, str, str, str]] = []

    for case in cases:
        total += 1
        result = simulate_turn(case)
        final_clean = guard_message(result["final_text"]).clean
        statuses_ok = result["statuses"] == case["expect_statuses"]
        if case["expect_final"] == "holding":
            final_ok = result["final_text"] == SAFE_FALLBACK_MESSAGE
        else:
            final_ok = result["final_text"] != SAFE_FALLBACK_MESSAGE and final_clean
        if not final_clean:
            leak_throughs += 1

        scenario_ok = statuses_ok and final_ok and final_clean
        passed += int(scenario_ok)
        rows.append(
            (
                case["id"],
                "->".join(result["statuses"]),
                "PASS" if scenario_ok else "FAIL",
                "clean" if final_clean else "LEAKED",
            )
        )

    return {
        "rows": rows,
        "total": total,
        "passed": passed,
        "leak_throughs": leak_throughs,
    }


# ==========================================================================
# Reporting
# ==========================================================================


def build_report(guard: dict, contracts: dict, loops: dict) -> str:
    lines = [
        "# Offline Eval Results",
        "",
        "Generated by `python evals/run_evals.py` (no API key required).",
        "Fixtures: `evals/scenarios.jsonl`.",
        "",
        "## 1. Deterministic guardrail - leak detection",
        "",
        "| Scenario | Draft verdict | Expected match | Detected categories |",
        "| --- | --- | --- | --- |",
    ]
    for case_id, clean, matched, categories in guard["rows"]:
        verdict = "clean" if clean else "BLOCKED"
        lines.append(
            f"| {case_id} | {verdict} | {'yes' if matched else 'NO'} | {categories} |"
        )
    lines += [
        "",
        f"- Leaky drafts blocked: **{guard['leaky_blocked']}/{guard['leaky_total']}** "
        f"({guard['block_rate']:.0%} block rate)",
        f"- Clean drafts passed: **{guard['clean_passed']}/{guard['clean_total']}** "
        f"({guard['specificity']:.0%} specificity)",
        f"- Category precision: {guard['precision']:.2f} / recall: {guard['recall']:.2f}",
        "",
        "## 2. Contract conformance (fail-closed handoffs)",
        "",
        "| Case | Expected | Observed |",
        "| --- | --- | --- |",
    ]
    for case_id, expected, observed in contracts["rows"]:
        lines.append(f"| {case_id} | {expected} | {observed} |")
    lines += [
        "",
        f"- Valid model outputs parsed: **{contracts['ok_passed']}/{contracts['ok_total']}**",
        f"- Broken outputs failed closed: **{contracts['bad_failed_closed']}/{contracts['bad_total']}**",
        "",
        "## 3. Generator-critic loop - convergence & safety",
        "",
        "| Scenario | Status path | Result | Final message |",
        "| --- | --- | --- | --- |",
    ]
    for case_id, path, result, final in loops["rows"]:
        lines.append(f"| {case_id} | {path} | {result} | {final} |")
    lines += [
        "",
        f"- Scenarios passed: **{loops['passed']}/{loops['total']}**",
        f"- **Internal-data leak-throughs to final messages: {loops['leak_throughs']}**",  # noqa: E501
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    cases = load_scenarios()
    guard = run_guard_suite([c for c in cases if c["suite"] == "guard"])
    contracts = run_contract_suite([c for c in cases if c["suite"] in ("verdict", "strategy")])
    loops = run_loop_suite([c for c in cases if c["suite"] == "loop"])

    report = build_report(guard, contracts, loops)
    RESULTS_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {RESULTS_PATH.relative_to(ROOT)}")

    thresholds = [
        ("leak block rate == 100%", guard["block_rate"] == 1.0),
        ("clean specificity == 100%", guard["specificity"] == 1.0),
        ("fail-closed on broken contracts == 100%", contracts["bad_failed_closed"] == contracts["bad_total"]),
        ("valid contracts parse == 100%", contracts["ok_passed"] == contracts["ok_total"]),
        ("loop scenarios == all", loops["passed"] == loops["total"]),
        ("final-message leak-throughs == 0", loops["leak_throughs"] == 0),
    ]
    print("\nHard safety thresholds:")
    for label, ok in thresholds:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return 0 if all(ok for _, ok in thresholds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
