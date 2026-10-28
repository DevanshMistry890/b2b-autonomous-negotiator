"""Unit tests for the generator-critic loop state machine (no LLM required)."""

from negotiator import loop_control as lc
from negotiator.guardrails import SAFE_FALLBACK_MESSAGE
from negotiator.schemas import canonical_json

CLEAN = (
    "We can't accept $4.50. Given your 0.65 reliability score, "
    "we need $4.00 to proceed."
)
LEAKY = "We can't accept $4.50 because our price ceiling is $4.25."

APPROVED_CLEAN = (
    '{"status": "approved", "feedback": "clean", "final_message": %s, "violations": []}'
    % f'"{CLEAN}"'
)
APPROVED_LEAKY = (
    '{"status": "approved", "feedback": "clean", "final_message": %s, "violations": []}'
    % f'"{LEAKY}"'
)
REJECTED = '{"status": "rejected", "feedback": "remove the leak", "final_message": null}'


def _turn():
    state = {}
    lc.begin_turn(state)
    return state


# ------------------------------------------------------------- turn lifecycle


def test_begin_turn_resets_loop_state_but_counts_turns():
    state = {}
    lc.begin_turn(state)
    state[lc.CURRENT_DRAFT_KEY] = "draft"
    state[lc.PREVIOUS_FEEDBACK_KEY] = "fb"
    state[lc.DRAFT_REVISION_KEY] = 2
    lc.begin_turn(state)
    assert lc.current_turn(state) == 2
    assert lc.current_revision(state) == 0
    assert lc.PREVIOUS_FEEDBACK_KEY not in state
    assert lc.CURRENT_DRAFT_KEY not in state
    assert lc.run_metrics(state)["draft_revisions"] == 0


def test_record_draft_revision_increments_and_tracks_metrics():
    state = _turn()
    assert lc.record_draft_revision(state) == 1
    assert lc.record_draft_revision(state) == 2
    assert lc.current_revision(state) == 2
    assert lc.run_metrics(state)["draft_revisions"] == 2


# -------------------------------------------------------------- resolve_audit


def test_clean_approval_passes_without_override():
    state = _turn()
    lc.record_draft_revision(state)
    verdict, override = lc.resolve_audit(state, APPROVED_CLEAN, draft_text=CLEAN)
    assert verdict.status == "approved"
    assert override is None
    assert state[lc.AUDIT_APPROVED_KEY] is True
    assert lc.run_metrics(state)["approved"] is True


def test_leaky_approval_is_vetoed_by_deterministic_firewall():
    """The core defense-in-depth claim: LLM approval is advisory."""
    state = _turn()
    lc.record_draft_revision(state)
    verdict, override = lc.resolve_audit(state, APPROVED_LEAKY, draft_text=LEAKY)
    assert verdict.status == "rejected"
    assert override is not None
    assert "firewall" in verdict.feedback.lower()
    assert state[lc.PREVIOUS_FEEDBACK_KEY]
    assert lc.run_metrics(state)["guard_hits"] > 0


def test_rejection_routes_feedback_to_the_drafter():
    state = _turn()
    lc.record_draft_revision(state)
    verdict, _ = lc.resolve_audit(state, REJECTED, draft_text=LEAKY)
    assert verdict.status == "rejected"
    assert state[lc.PREVIOUS_FEEDBACK_KEY] == "remove the leak"
    assert lc.run_metrics(state)["audit_rejections"] == 1


def test_unparseable_verdict_fails_closed_with_override():
    state = _turn()
    lc.record_draft_revision(state)
    verdict, override = lc.resolve_audit(state, "not json at all", draft_text=CLEAN)
    assert verdict.status == "rejected"
    assert override is not None
    assert "verdict_contract_violation" in verdict.violations


def test_approval_without_final_message_fails_closed():
    state = _turn()
    lc.record_draft_revision(state)
    verdict, override = lc.resolve_audit(
        state, '{"status": "approved", "feedback": "fine"}', draft_text=CLEAN
    )
    assert verdict.status == "rejected"
    assert override is not None
    assert "missing_final_message" in verdict.violations


def test_escalation_after_revision_budget():
    state = _turn()
    for _ in range(lc.MAX_DRAFT_REVISIONS):
        lc.record_draft_revision(state)
        verdict, _ = lc.resolve_audit(state, REJECTED, draft_text=LEAKY)
    assert verdict.status == "escalated"
    assert verdict.final_message == SAFE_FALLBACK_MESSAGE
    assert state[lc.ESCALATION_KEY] is True
    assert lc.run_metrics(state)["escalated"] is True


def test_approval_at_revision_budget_still_approves():
    state = _turn()
    for _ in range(lc.MAX_DRAFT_REVISIONS - 1):
        lc.record_draft_revision(state)
        lc.resolve_audit(state, REJECTED, draft_text=LEAKY)
    lc.record_draft_revision(state)
    verdict, _ = lc.resolve_audit(state, APPROVED_CLEAN, draft_text=CLEAN)
    assert verdict.status == "approved"


def test_status_path_records_every_resolved_verdict():
    state = _turn()
    lc.record_draft_revision(state)
    lc.resolve_audit(state, REJECTED, draft_text=LEAKY)
    lc.record_draft_revision(state)
    lc.resolve_audit(state, APPROVED_CLEAN, draft_text=CLEAN)
    assert lc.run_metrics(state)["status_path"] == ["rejected", "approved"]


def test_canonical_verdict_is_written_to_state():
    state = _turn()
    lc.record_draft_revision(state)
    verdict, _ = lc.resolve_audit(state, APPROVED_CLEAN, draft_text=CLEAN)
    assert state[lc.AUDIT_RESULT_KEY] == canonical_json(verdict)


# -------------------------------------------------------- output firewall


def test_firewall_replaces_leaking_final_output():
    state = _turn()
    override = lc.enforce_output_firewall(state, "Sure. Our ceiling is $4.25.")
    assert override is not None
    assert override.parts[0].text == SAFE_FALLBACK_MESSAGE
    assert lc.run_metrics(state)["firewall_trips"] == 1
    assert not state.get(lc.ESCALATION_KEY)  # holding message, honest state


def test_firewall_passes_clean_output_untouched():
    assert lc.enforce_output_firewall(_turn(), CLEAN) is None
    assert lc.enforce_output_firewall(_turn(), "   ") is None
