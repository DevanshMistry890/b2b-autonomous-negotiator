"""B2B Autonomous Procurement Negotiator — agent assembly (Google ADK 2.x).

Hub-and-Spoke Triumvirate: a silent Root Orchestrator delegates to three
specialized, stateless sub-agents wrapped as :class:`AgentTool`:

    Strategist  — queries the mock ERP, emits a typed StrategyDirective
    Drafter     — turns the directive into one concise vendor message
    Auditor     — compliance gate; returns a typed AuditVerdict

Three independent safety layers surround the Generator-Critic loop, in
increasing order of authority:

1. **Prompt boundaries** (``instructions.py``) — role-scoped instructions
   and few-shot examples. Cheap, advisory.
2. **LLM Auditor** — generator-critic review with deterministic feedback
   routing back into the Drafter. Advisory (models can be fooled).
3. **Deterministic firewall** (``guardrails.py`` + ``loop_control.py``) —
   plain-Python leak scanning with *authority*: it can veto an approval,
   force escalation past the revision budget, and replace any final output
   that quotes internal data. It runs in callbacks, where the model cannot
   skip or argue with it.

Hook placement is deliberate (and cheap to explain in an interview):

* ``before_agent_callback`` (root)   -> :func:`loop_control.begin_turn`
* ``after_agent_callback`` (drafter) -> :func:`loop_control.record_draft_revision`
* ``after_model_callback`` (auditor) -> :func:`loop_control.resolve_audit`
  ...in ``after_model_callback`` because in ADK 2.x ``after_agent_callback``
  *appends* an event, while ``after_model_callback`` can *replace* the
  response before it is persisted — an enforcement point must replace, not
  append.
* ``after_model_callback`` (root)    -> :func:`loop_control.enforce_output_firewall`

Everything deciding *policy* lives in ``loop_control`` as pure functions so
the whole loop can be evaluated offline (see ``evals/run_evals.py``);
everything here is a thin adapter to ADK.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from . import loop_control as lc
from .instructions import (
    ORCHESTRATOR_INSTRUCTION,
    auditor_instruction,
    drafter_instruction,
    strategist_instruction,
)
from .schemas import AuditVerdict, StrategyDirective
from .tools import (
    get_component_baseline,
    get_inventory_runway,
    get_negotiation_mandate,
    get_supplier_reliability,
    scan_for_data_leaks,
)

logger = logging.getLogger("negotiator")

#: One model constant for the whole triumvirate — cheap agents, fast loop.
#: Override per-environment without code changes:
#:     NEGOTIATOR_MODEL=gemini-2.5-flash adk api_server
MODEL = os.environ.get("NEGOTIATOR_MODEL", "gemini-2.5-flash")


# ==================== CALLBACK ADAPTERS ====================
# Thin adapters only: policy lives in loop_control (pure, unit-tested).


def _response_text(response: LlmResponse) -> str:
    """Join the visible (non-thought) text parts of a model response."""
    if response.content is None or response.content.parts is None:
        return ""
    return "".join(
        part.text or ""
        for part in response.content.parts
        if not part.thought and part.text
    )


def _is_final_text_response(response: LlmResponse) -> bool:
    """True when the response is the agent's closing message (text-only):
    no tool calls attached, so the agentic loop ends here."""
    if response.content is None or response.content.parts is None:
        return False
    has_text = False
    for part in response.content.parts:
        if part.function_call or part.function_response:
            return False
        if not part.thought and part.text:
            has_text = True
    return has_text


def before_orchestrator(callback_context: CallbackContext) -> types.Content | None:
    """Reset turn-scoped loop state once per user message."""
    lc.begin_turn(callback_context.session.state)
    return None


def after_drafter(callback_context: CallbackContext) -> types.Content | None:
    """Count the revision so the loop budget is enforced in code, not prose."""
    revision = lc.record_draft_revision(callback_context.session.state)
    logger.info("Drafter produced revision %d", revision)
    return None


def after_model_auditor(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Resolve the Auditor's final verdict through the deterministic gate.

    Replaces the model response when enforcement disagrees (firewall veto,
    contract violation, or escalation) so the orchestrator only ever sees
    the enforced verdict.
    """
    if not _is_final_text_response(llm_response):
        return None  # mid-loop tool-call round: leave the pipeline alone

    state = callback_context.session.state
    draft_text = str(state.get(lc.CURRENT_DRAFT_KEY) or "")
    verdict, override = lc.resolve_audit(
        state, _response_text(llm_response), draft_text=draft_text
    )
    logger.info(
        "Audit resolved: status=%s violations=%s", verdict.status, verdict.violations
    )
    if override is None:
        return None
    return LlmResponse(content=override.content)


def after_model_orchestrator(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Output firewall: nothing user-facing ships without a clean scan.

    The last line of defense — even if every upstream layer approved, a
    message that quotes internal data is replaced with a safe holding text.
    """
    if not _is_final_text_response(llm_response):
        return None
    state = callback_context.session.state
    override = lc.enforce_output_firewall(state, _response_text(llm_response))
    if override is None:
        return None
    logger.warning("Output firewall intercepted a leaking message")
    return LlmResponse(content=override.content)


# ==================== SPECIALIST AGENTS ====================
# Stateless LlmAgents: all coordination flows through typed outputs
# (output_schema) and session state (output_key + instruction providers).


strategist_agent = LlmAgent(
    model=MODEL,
    name="Strategist",
    description="Analyzes supply chain data and vendor communications to dictate negotiation tactics.",
    instruction=strategist_instruction,
    output_key=lc.STRATEGY_KEY,
    output_schema=StrategyDirective,
    tools=[
        get_component_baseline,
        get_supplier_reliability,
        get_inventory_runway,
        get_negotiation_mandate,
    ],
)

drafter_agent = LlmAgent(
    model=MODEL,
    name="Drafter",
    description="Translates strategic JSON directives into professional B2B vendor communications.",
    instruction=drafter_instruction,
    output_key=lc.CURRENT_DRAFT_KEY,
    after_agent_callback=after_drafter,
)

auditor_agent = LlmAgent(
    model=MODEL,
    name="Auditor",
    description="Acts as a compliance firewall, evaluating drafted messages for data leaks or policy violations.",
    instruction=auditor_instruction,
    output_key=lc.AUDIT_RESULT_KEY,
    output_schema=AuditVerdict,
    tools=[scan_for_data_leaks],
    after_model_callback=after_model_auditor,
)


# ==================== ROOT ORCHESTRATOR ====================


root_agent = LlmAgent(
    model=MODEL,
    name="procurement_orchestrator",
    instruction=ORCHESTRATOR_INSTRUCTION,
    # Sub-agents as tools: their internals never surface in the chat —
    # the orchestrator is the only user-facing agent.
    tools=[
        AgentTool(strategist_agent),
        AgentTool(drafter_agent),
        AgentTool(auditor_agent),
    ],
    before_agent_callback=before_orchestrator,
    after_model_callback=after_model_orchestrator,
)
