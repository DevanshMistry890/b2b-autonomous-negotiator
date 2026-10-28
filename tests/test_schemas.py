"""Unit tests for the typed inter-agent contracts and JSON extraction."""

import pytest

from negotiator.schemas import (
    CONTRACT_VIOLATION_MARKERS,
    AuditVerdict,
    StrategyDirective,
    canonical_json,
    extract_json,
    parse_strategy,
    parse_verdict,
    parse_verdict_failsafe,
)


# ---------------------------------------------------------------- extract_json


def test_extract_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_fenced_object():
    text = '```json\n{"status": "approved"}\n```'
    assert extract_json(text) == {"status": "approved"}


def test_extract_object_with_surrounding_prose():
    text = 'Here is the strategy: {"analysis": "x", "n": 2} hope that helps!'
    assert extract_json(text) == {"analysis": "x", "n": 2}


def test_extract_nested_object():
    text = 'noise {"outer": {"inner": [1, 2, {"deep": true}]}, "z": 0} trailing'
    assert extract_json(text) == {"outer": {"inner": [1, 2, {"deep": True}]}, "z": 0}


def test_extract_handles_braces_inside_strings():
    text = '{"msg": "we use {curly} braces", "ok": true}'
    assert extract_json(text) == {"msg": "we use {curly} braces", "ok": True}


def test_extract_dict_passthrough():
    assert extract_json({"already": "decoded"}) == {"already": "decoded"}


@pytest.mark.parametrize("bad", ["", "   ", "no json here", "{broken", None, 42])
def test_extract_rejects_garbage(bad):
    with pytest.raises(ValueError):
        extract_json(bad)


# ---------------------------------------------------------- StrategyDirective


VALID_STRATEGY = {
    "analysis": "Vendor demands $4.50; their reliability is weak.",
    "selected_tactic": "call_bluff",
    "target_price": 4.0,
    "max_concession": 4.25,
    "instructions_for_drafter": ["Reject $4.50.", "Cite reliability score."],
    "tool_evidence": ["get_supplier_reliability(v_cheap_supplies)"],
}


def test_parse_strategy_valid():
    directive = parse_strategy(VALID_STRATEGY)
    assert isinstance(directive, StrategyDirective)
    assert directive.selected_tactic == "call_bluff"
    assert directive.target_price == 4.0


def test_parse_strategy_normalizes_human_tactic_labels():
    payload = dict(VALID_STRATEGY, selected_tactic="Call Bluff")
    assert parse_strategy(payload).selected_tactic == "call_bluff"


def test_parse_strategy_coerces_currency_strings():
    payload = dict(VALID_STRATEGY, target_price="$4.00", max_concession="4,250.50")
    directive = parse_strategy(payload)
    assert directive.target_price == 4.0
    assert directive.max_concession == 4250.5  # comma = thousands separator


@pytest.mark.parametrize(
    "mutation",
    [
        {"instructions_for_drafter": []},
        {"instructions_for_drafter": ["  "]},
        {"instructions_for_drafter": "not a list"},
        {"analysis": ""},
        {"selected_tactic": ""},
        {"target_price": -1.0},
    ],
)
def test_parse_strategy_contract_violations(mutation):
    payload = {**VALID_STRATEGY, **mutation}
    with pytest.raises(ValueError):
        parse_strategy(payload)


# --------------------------------------------------------------- AuditVerdict


def test_parse_verdict_valid_approved():
    verdict = parse_verdict(
        '{"status": "approved", "feedback": "clean", "final_message": "Hi.", "violations": []}'
    )
    assert isinstance(verdict, AuditVerdict)
    assert verdict.status == "approved"
    assert verdict.final_message == "Hi."


def test_parse_verdict_fenced_and_prosey():
    text = 'Sure!\n```json\n{"status": "rejected", "feedback": "leak"}\n```\nDone.'
    assert parse_verdict(text).status == "rejected"


@pytest.mark.parametrize(
    "raw",
    [
        "complete garbage",
        '{"status": "maybe"}',
        '{"feedback": "no status"}',
    ],
)
def test_failsafe_rejects_contract_violations(raw):
    verdict = parse_verdict_failsafe(raw)
    assert verdict.status == "rejected"
    assert set(verdict.violations) & set(CONTRACT_VIOLATION_MARKERS)


def test_failsafe_approved_without_final_message_is_rejected():
    verdict = parse_verdict_failsafe('{"status": "approved", "feedback": "looks fine"}')
    assert verdict.status == "rejected"
    assert "missing_final_message" in verdict.violations


def test_failsafe_passes_valid_verdict_through():
    verdict = parse_verdict_failsafe(
        '{"status": "approved", "final_message": "ok"}'
    )
    assert verdict.status == "approved"


# ------------------------------------------------------------- canonical_json


def test_canonical_json_is_deterministic_and_sorted():
    verdict = AuditVerdict(status="approved", feedback="x", final_message="m")
    assert canonical_json(verdict) == (
        '{"feedback": "x", "final_message": "m", "status": "approved", "violations": []}'
    )
