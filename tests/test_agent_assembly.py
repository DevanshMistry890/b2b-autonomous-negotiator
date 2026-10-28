"""Structural tests for the ADK agent assembly (construction only, no API)."""

from google.adk.tools.agent_tool import AgentTool

from negotiator import agent
from negotiator.agent import (
    auditor_agent,
    drafter_agent,
    root_agent,
    strategist_agent,
)
from negotiator.schemas import AuditVerdict, StrategyDirective


def _tool_names(tools):
    return [getattr(t, "name", None) or t.__name__ for t in tools]


def test_root_wraps_the_triumvirate_as_agent_tools():
    assert root_agent.name == "procurement_orchestrator"
    assert all(isinstance(t, AgentTool) for t in root_agent.tools)
    assert _tool_names(root_agent.tools) == ["Strategist", "Drafter", "Auditor"]


def test_strategist_queries_erp_and_emits_typed_directive():
    assert strategist_agent.output_key == "strategy"
    assert strategist_agent.output_schema is StrategyDirective
    assert _tool_names(strategist_agent.tools) == [
        "get_component_baseline",
        "get_supplier_reliability",
        "get_inventory_runway",
        "get_negotiation_mandate",
    ]


def test_drafter_counts_revisions_via_callback():
    assert drafter_agent.output_key == "current_draft"
    assert drafter_agent.after_agent_callback is agent.after_drafter


def test_auditor_is_schema_bound_with_explainability_tool():
    assert auditor_agent.output_key == "audit_result"
    assert auditor_agent.output_schema is AuditVerdict
    assert _tool_names(auditor_agent.tools) == ["scan_for_data_leaks"]
    assert auditor_agent.after_model_callback is agent.after_model_auditor


def test_root_has_both_enforcement_hooks():
    assert root_agent.before_agent_callback is agent.before_orchestrator
    assert root_agent.after_model_callback is agent.after_model_orchestrator


def test_instructions_are_state_aware_providers():
    for sub_agent in (strategist_agent, drafter_agent, auditor_agent):
        assert callable(sub_agent.instruction)
    assert isinstance(root_agent.instruction, str)


def test_model_is_env_overridable_constant():
    assert isinstance(agent.MODEL, str) and agent.MODEL
