"""Unit tests for the mock ERP tool layer and the data-boundary split."""

from negotiator.tools import (
    MOCK_ERP,
    SCENARIO_COMPONENT_ID,
    SCENARIO_SUPPLIER_ID,
    get_component_baseline,
    get_inventory_runway,
    get_negotiation_mandate,
    get_supplier_reliability,
    scan_for_data_leaks,
)


def test_component_baseline_returns_commercial_data():
    data = get_component_baseline(SCENARIO_COMPONENT_ID)
    assert data["component_id"] == SCENARIO_COMPONENT_ID
    assert data["baseline_unit_price"] > 0
    assert "market_index_trend" in data


def test_supplier_reliability_returns_scorecard():
    data = get_supplier_reliability(SCENARIO_SUPPLIER_ID)
    assert data["reliability_score"] == 0.65
    assert data["historical_incidents"]


def test_unknown_ids_fail_soft_with_known_ids():
    for fn in (get_component_baseline, get_inventory_runway, get_negotiation_mandate):
        assert "error" in fn("CMP-0000")
    assert "error" in get_supplier_reliability("v_nobody")


def test_inventory_runway_is_marked_internal():
    data = get_inventory_runway(SCENARIO_COMPONENT_ID)
    assert data["inventory_runway_days"] == 14
    assert "INTERNAL" in data["classification"]


def test_mandate_is_marked_internal_and_carries_ceiling():
    data = get_negotiation_mandate(SCENARIO_COMPONENT_ID)
    assert data["max_price_ceiling"] == 4.25
    assert "INTERNAL" in data["classification"]


def test_data_boundary_baseline_exposes_no_ceiling_or_runway():
    """The shareable tool must not smuggle internal metrics."""
    data = get_component_baseline(SCENARIO_COMPONENT_ID)
    assert "max_price_ceiling" not in data
    assert "inventory_runway_days" not in data
    assert "walk_away_conditions" not in data


def test_mock_data_is_consistent_across_tools():
    component = MOCK_ERP["components"][SCENARIO_COMPONENT_ID]
    mandate = MOCK_ERP["mandates"][SCENARIO_COMPONENT_ID]
    assert component["max_price_ceiling"] == mandate["max_price_ceiling"]


def test_scan_for_data_leaks_exposes_firewall_as_tool():
    result = scan_for_data_leaks("Our price ceiling is $4.25.")
    assert result["clean"] is False
    assert result["findings"]
    clean = scan_for_data_leaks("We propose $4.00.")
    assert clean["clean"] is True
    assert clean["findings"] == []
