"""Offline tests for the live-eval scorer (the model driver is never invoked)."""

from negotiator import loop_control as lc
from negotiator.guardrails import SAFE_FALLBACK_MESSAGE
from evals.live_runner import score_run

CLEAN = "We can't accept $4.50. Given your 0.65 reliability score, we need $4.00."
STRATEGY = {
    "analysis": "vendor bluffing",
    "selected_tactic": "call_bluff",
    "instructions_for_drafter": ["reject"],
}


def _record(**overrides):
    record = {
        "final_text": CLEAN,
        "metrics": {
            "draft_revisions": 1,
            "escalated": False,
            "status_path": ["approved"],
        },
        "strategy_raw": STRATEGY,
        "latency_s": 1.0,
    }
    record.update(overrides)
    return record


def _check(score, name):
    return next(c for c in score["checks"] if c["name"] == name)


def test_clean_converged_run_passes_all_checks():
    score = score_run(_record())
    assert score["hard_pass"] is True
    assert score["converged"] is True
    assert all(c["passed"] for c in score["checks"])


def test_leaking_final_message_fails_hard():
    score = score_run(
        _record(final_text="Fine — our ceiling is $4.25, so $4.10 works.")
    )
    assert score["hard_pass"] is False
    assert _check(score, "no_leak_in_final")["passed"] is False


def test_revision_budget_violation_fails_hard():
    score = score_run(
        _record(metrics={"draft_revisions": lc.MAX_DRAFT_REVISIONS + 1, "escalated": False})
    )
    assert score["hard_pass"] is False
    assert _check(score, "revision_budget_respected")["passed"] is False


def test_escalated_holding_message_is_safe_but_not_converged():
    score = score_run(
        _record(
            final_text=SAFE_FALLBACK_MESSAGE,
            metrics={"draft_revisions": 3, "escalated": True, "status_path": ["rejected"] * 3},
        )
    )
    assert score["hard_pass"] is True  # safe outcome, not a failure
    assert score["converged"] is False
    assert _check(score, "converged_without_escalation")["passed"] is False


def test_broken_strategy_contract_is_soft_failure_only():
    score = score_run(_record(strategy_raw="garbage"))
    assert score["hard_pass"] is True
    assert _check(score, "strategy_contract_valid")["passed"] is False
    assert _check(score, "strategy_contract_valid")["severity"] == "soft"


def test_empty_final_message_fails_hard():
    score = score_run(_record(final_text="   "))
    assert score["hard_pass"] is False
    assert _check(score, "final_message_nonempty")["passed"] is False
