"""Unit tests for the deterministic compliance firewall."""

import pytest

from negotiator.guardrails import (
    CATEGORY_EMAIL_FORMAT,
    CATEGORY_INTERNAL_METRIC,
    CATEGORY_MARGIN,
    CATEGORY_PRICE_CEILING,
    CATEGORY_STATE_KEY,
    CRITICAL,
    SAFE_FALLBACK_MESSAGE,
    WARNING,
    guard_message,
    scan_text,
)

CLEAN_DRAFTS = [
    "We can't accept $4.50. Given your 0.65 reliability score and the risk of late deliveries, we need $4.00 to proceed.",
    "Your quote is above current market conditions. We propose $4.00 with Net-60 terms to move forward.",
    "We appreciate the offer, but $4.50 doesn't reflect the market trend. $4.00 is where we can sign today.",
    "Given the competing quotes on this component, we need $4.00 to proceed with your offer.",
    "Net-30 terms are too tight for us. Net-60 at $4.00 works.",
]

LEAKY_DRAFTS = [
    (
        "We can't accept $4.50. Our maximum price ceiling is $4.25, so we need $4.00.",
        {CATEGORY_PRICE_CEILING},
    ),
    (
        "Your price doesn't work — we only have 14 days of inventory left and our runway is short.",
        {CATEGORY_INTERNAL_METRIC},
    ),
    (
        "Our internal analysis shows the cost basis is $3.20, so $4.00 still works for us.",
        {CATEGORY_INTERNAL_METRIC},
    ),
    (
        "Stay under the price_ceiling and we have a deal at $4.00.",
        {CATEGORY_PRICE_CEILING},
    ),
    (
        "Our walk-away is $4.25. Take $4.00 or we pivot to another supplier.",
        {CATEGORY_PRICE_CEILING},
    ),
    (
        "Fine — the inventory_runway pressure means we can stretch to $4.10.",
        {CATEGORY_INTERNAL_METRIC},
    ),
    (
        "The burn rate impact forces us to ask for $4.00.",
        {CATEGORY_INTERNAL_METRIC},
    ),
]


def test_clean_drafts_produce_no_findings():
    for draft in CLEAN_DRAFTS:
        report = guard_message(draft)
        assert report.findings == [], f"unexpected findings for: {draft!r}"
        assert report.clean


def test_safe_fallback_message_is_clean():
    assert guard_message(SAFE_FALLBACK_MESSAGE).clean


@pytest.mark.parametrize("draft,categories", LEAKY_DRAFTS)
def test_leaky_drafts_are_blocked_with_expected_categories(draft, categories):
    report = guard_message(draft)
    assert not report.clean, f"leak not blocked: {draft!r}"
    detected = {f.category for f in report.findings}
    assert categories <= detected, f"missing {categories - detected} in {draft!r}"
    assert all(f.severity == CRITICAL for f in report.blocking_findings)


def test_warning_findings_are_recorded_but_do_not_block():
    draft = "Subject: Counter-offer\nDear Vendor, we propose $4.00. Sincerely, Procurement."
    report = guard_message(draft)
    assert report.clean  # format/tone is the LLM Auditor's job, not the hard gate
    assert {f.category for f in report.findings} == {CATEGORY_EMAIL_FORMAT}
    assert all(f.severity == WARNING for f in report.findings)


def test_margin_word_is_warning_severity():
    report = guard_message("Margins are tight on both sides at $4.10.")
    assert report.clean
    assert [f.category for f in report.findings] == [CATEGORY_MARGIN]


def test_finding_spans_are_accurate():
    draft = "Our price ceiling is $4.25 and that is final."
    findings = scan_text(draft)
    assert findings
    for finding in findings:
        assert draft[finding.start : finding.end] == finding.excerpt


def test_findings_carry_excerpts_for_explainability():
    report = guard_message("We have 14 days of inventory left.")
    excerpts = [f.excerpt.lower() for f in report.findings]
    assert any("inventory" in excerpt for excerpt in excerpts)


def test_empty_text_is_clean():
    assert scan_text("") == []
    assert guard_message("").clean
