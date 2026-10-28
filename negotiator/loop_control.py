"""Pure state-machine logic for the Generator-Critic loop.

Everything the negotiation loop *decides* lives here as plain functions over
a state mapping; the ADK callbacks in :mod:`negotiator.agent` are thin
adapters. That split is deliberate: the loop can be unit-tested and
simulated end-to-end without an LLM, an API key, or an ADK runtime — which
is exactly what ``evals/run_evals.py`` does.

State contract (one negotiation turn):

    strategy          the Strategist's directive (JSON str or dict)
    current_draft     the Drafter's latest draft text
    audit_result      the canonical audit verdict (JSON str)
    previous_feedback Auditor feedback the Drafter must apply next revision
    draft_revision    monotonically increasing per Drafter invocation
    audit_approved    True once a leak-free draft is approved
    escalation        True once the revision budget is exhausted
    run_metrics       counters surfaced to the UI telemetry pane

Loop invariants (enforced here, not in prompts):

1. Unparseable verdicts are rejections (fail closed).
2. An "approved" verdict is void if the deterministic firewall finds a
   critical leak in the text (the model never overrules the guard).
3. At most ``MAX_DRAFT_REVISIONS`` drafts per turn; beyond that the loop
   escalates to a human with a safe holding message instead of looping.
4. Nothing user-facing ships unless :func:`enforce_output_firewall` sees
   clean text.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, MutableMapping

from google.genai import types

from .guardrails import SAFE_FALLBACK_MESSAGE, guard_message
from .schemas import (
    CONTRACT_VIOLATION_MARKERS,
    AuditVerdict,
    canonical_json,
    parse_verdict_failsafe,
)

# ---- Loop budget ---------------------------------------------------------

#: Maximum Drafter->Auditor cycles per user turn before escalation.
MAX_DRAFT_REVISIONS = 3

# ---- State keys (the contract shared by all agents) ----------------------

STRATEGY_KEY = "strategy"
CURRENT_DRAFT_KEY = "current_draft"
AUDIT_RESULT_KEY = "audit_result"
PREVIOUS_FEEDBACK_KEY = "previous_feedback"
DRAFT_REVISION_KEY = "draft_revision"
AUDIT_APPROVED_KEY = "audit_approved"
ESCALATION_KEY = "escalation"
RUN_METRICS_KEY = "run_metrics"
TURN_KEY = "negotiation_turn"

_STATE_KEYS_PER_TURN = (
    CURRENT_DRAFT_KEY,
    AUDIT_RESULT_KEY,
    PREVIOUS_FEEDBACK_KEY,
    DRAFT_REVISION_KEY,
    AUDIT_APPROVED_KEY,
    ESCALATION_KEY,
)


# --------------------------------------------------------------------------
# Small state helpers
# --------------------------------------------------------------------------


def current_turn(state: Mapping[str, Any]) -> int:
    """1-based negotiation turn counter (persisted across turns of a session)."""
    value = state.get(TURN_KEY, 0)
    return value if isinstance(value, int) else 0


def current_revision(state: Mapping[str, Any]) -> int:
    """Number of drafts produced so far in the current turn."""
    value = state.get(DRAFT_REVISION_KEY, 0)
    return value if isinstance(value, int) else 0


def run_metrics(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the per-turn run metrics (never the live object)."""
    metrics = state.get(RUN_METRICS_KEY)
    if isinstance(metrics, dict):
        return dict(metrics)
    return {
        "draft_revisions": 0,
        "audit_rejections": 0,
        "guard_hits": 0,
        "escalated": False,
        "firewall_trips": 0,
        "approved": False,
        "status_path": [],
    }


def _mutate_metrics(state: MutableMapping[str, Any], **updates: Any) -> None:
    metrics = run_metrics(state)
    metrics.update(updates)
    state[RUN_METRICS_KEY] = metrics


def _json_part(payload: dict[str, Any]) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=json.dumps(payload))])


# --------------------------------------------------------------------------
# Turn lifecycle
# --------------------------------------------------------------------------


def begin_turn(state: MutableMapping[str, Any]) -> dict[str, Any]:
    """Reset turn-scoped state and seed metrics. Called from the root
    agent's ``before_agent_callback`` once per user message.

    Cross-turn state (``strategy``, turn counter) survives so a multi-turn
    negotiation keeps its memory; loop bookkeeping does not.
    """
    state[TURN_KEY] = current_turn(state) + 1
    for key in _STATE_KEYS_PER_TURN:
        state.pop(key, None)
    metrics = run_metrics(state)
    metrics.update(
        {
            "draft_revisions": 0,
            "audit_rejections": 0,
            "guard_hits": 0,
            "escalated": False,
            "firewall_trips": 0,
            "approved": False,
            "status_path": [],
        }
    )
    state[RUN_METRICS_KEY] = metrics
    return run_metrics(state)


def record_draft_revision(state: MutableMapping[str, Any]) -> int:
    """Count one Drafter invocation. Called from the Drafter's
    ``after_agent_callback``. Returns the new revision number."""
    revision = current_revision(state) + 1
    state[DRAFT_REVISION_KEY] = revision
    metrics = run_metrics(state)
    metrics["draft_revisions"] = revision
    state[RUN_METRICS_KEY] = metrics
    return revision


# --------------------------------------------------------------------------
# Audit resolution (the generator-critic gate)
# --------------------------------------------------------------------------


def resolve_audit(
    state: MutableMapping[str, Any],
    raw_verdict: Any,
    draft_text: str = "",
) -> tuple[AuditVerdict, types.Content | None]:
    """Resolve one Auditor turn into a final, enforced verdict.

    Combines the LLM verdict with the deterministic firewall and the
    revision budget, writes the outcome to ``state``, and returns an output
    override whenever the enforced verdict differs from what the Auditor
    said.

    Args:
        state: mutable session state (parent session deltas propagate).
        raw_verdict: the Auditor's raw output (JSON str or dict).
        draft_text: the draft under review, used when the verdict omits
            ``final_message``.

    Returns:
        ``(verdict, override)`` — ``override`` is a replacement
        :class:`types.Content` when enforcement changed the verdict, else
        ``None`` so the normal flow is untouched.
    """
    # Fail closed: the failsafe rejects anything that broke the verdict
    # contract (garbage, unknown status, approval without final_message),
    # tagging it with a marker from CONTRACT_VIOLATION_MARKERS.
    verdict = parse_verdict_failsafe(raw_verdict)
    contract_broken = bool(set(verdict.violations) & set(CONTRACT_VIOLATION_MARKERS))
    revision = current_revision(state)
    metrics = run_metrics(state)
    override: types.Content | None = None

    # 1. Deterministic firewall: scan both the approved text and the draft.
    scanned_text = (verdict.final_message or "").strip() or draft_text.strip()
    report = guard_message(scanned_text)
    findings = [f.to_dict() for f in report.findings]
    if report.findings:
        metrics["guard_hits"] += len(report.findings)

    # 2. Contract failures replace the model's raw output with the canonical
    #    verdict on the wire. A leak voids any approval — the model never
    #    overrules the guard.
    if contract_broken:
        override = _json_part(verdict.model_dump())
    if verdict.status == "approved" and not report.clean:
        verdict = AuditVerdict(
            status="rejected",
            feedback=(
                "Deterministic firewall veto (the LLM approval is void): "
                + report.summary()
                + " Rewrite without any internal metrics."
            ),
            final_message=None,
            violations=[f.category for f in report.blocking_findings],
        )
        override = _json_part(verdict.model_dump())

    # 3. Revision budget: after MAX_DRAFT_REVISIONS rejections, stop looping
    #    and escalate to a human with a safe holding message.
    if verdict.status == "rejected" and revision >= MAX_DRAFT_REVISIONS:
        verdict = AuditVerdict(
            status="escalated",
            feedback=(
                f"Revision budget exhausted after {revision} attempts. "
                "Escalating to a human negotiator with a holding message. "
                "Last feedback: " + verdict.feedback
            ),
            final_message=SAFE_FALLBACK_MESSAGE,
            violations=verdict.violations + ["revision_budget_exhausted"],
        )
        override = _json_part(verdict.model_dump())
        metrics["escalated"] = True

    # 4. Bookkeeping (always written, even when the override replaces the
    #    auditor's output — state is the durable record).
    metrics.setdefault("status_path", []).append(verdict.status)
    state[AUDIT_RESULT_KEY] = canonical_json(verdict)
    if verdict.status == "approved":
        state[AUDIT_APPROVED_KEY] = True
        metrics["approved"] = True
    elif verdict.status == "rejected":
        state[PREVIOUS_FEEDBACK_KEY] = verdict.feedback
        metrics["audit_rejections"] += 1
    else:  # escalated
        state[ESCALATION_KEY] = True
        state[PREVIOUS_FEEDBACK_KEY] = verdict.feedback

    if findings:
        state["guard_findings"] = findings
    state[RUN_METRICS_KEY] = metrics
    return verdict, override


# --------------------------------------------------------------------------
# Output firewall (last line of defense)
# --------------------------------------------------------------------------


def enforce_output_firewall(
    state: MutableMapping[str, Any],
    text: str,
) -> types.Content | None:
    """Deterministically gate the final user-facing message.

    Called from the root agent's ``after_agent_callback``. If the outgoing
    text contains any critical leak (even after an approved audit — defense
    in depth), it is replaced with the safe holding message.

    Args:
        state: mutable session state (metrics are recorded there).
        text: the exact text the root agent is about to emit.

    Returns:
        Replacement :class:`types.Content` when the message must not ship,
        otherwise ``None``.
    """
    if not text.strip():
        return None
    report = guard_message(text)
    if report.clean:
        return None

    metrics = run_metrics(state)
    metrics["firewall_trips"] += 1
    metrics["guard_hits"] += len(report.findings)
    state[RUN_METRICS_KEY] = metrics
    state["guard_findings"] = [f.to_dict() for f in report.findings]

    replacement = AuditVerdict(
        status="escalated",
        feedback="Output firewall intercepted a leaking message: " + report.summary(),
        final_message=SAFE_FALLBACK_MESSAGE,
        violations=[f.category for f in report.blocking_findings],
    )
    state[AUDIT_RESULT_KEY] = canonical_json(replacement)
    return types.Content(
        role="model",
        parts=[types.Part(text=SAFE_FALLBACK_MESSAGE)],
    )
