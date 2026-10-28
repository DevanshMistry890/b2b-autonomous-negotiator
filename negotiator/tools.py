"""Deterministic mock-ERP tools for the Strategist, plus the explainability
tool for the Auditor.

These are plain Python functions: Google ADK derives their JSON schemas from
the type hints and Google-style docstrings, and they run locally with no
network access. They simulate the internal systems of record a real
procurement team would query (ERP inventory, supplier scorecards, sourcing
mandate).

Data-boundary design (mirrors a real supply-chain stack):

* ``get_component_baseline`` / ``get_supplier_reliability`` — commercial
  data the Drafter may *reference* (e.g. a supplier's own reliability score).
* ``get_inventory_runway`` / ``get_negotiation_mandate`` — INTERNAL data:
  runway and price ceilings. The Strategist may reason over them; the
  Drafter is contractually forbidden to communicate them, and the
  deterministic firewall blocks any message that quotes them.

``scan_for_data_leaks`` exposes the deterministic firewall as a tool so the
Auditor can cite exact violations in its verdict. It is an *explainability*
surface only — enforcement runs in code (``loop_control``), where the model
cannot skip it.
"""

from __future__ import annotations

from typing import Any

from .guardrails import guard_message

# --------------------------------------------------------------------------
# Mock system-of-record data (fixed, so demos and evals are reproducible)
# --------------------------------------------------------------------------

SCENARIO_COMPONENT_ID = "CMP-4410"
SCENARIO_SUPPLIER_ID = "v_cheap_supplies"

MOCK_ERP: dict[str, dict[str, Any]] = {
    "components": {
        "CMP-4410": {
            "name": "Industrial Servo Motor (24V, 200W)",
            "baseline_unit_price": 4.20,
            "target_unit_price": 3.95,
            "max_price_ceiling": 4.25,
            "inventory_runway_days": 14,
            "weekly_consumption_units": 8500,
            "safety_stock_days": 10,
            "open_supplier_quotes": 3,
            "market_index_trend": "down 2.1% QoQ",
        },
        "CMP-2210": {
            "name": "Precision Linear Rail (1.2m)",
            "baseline_unit_price": 18.40,
            "target_unit_price": 17.10,
            "max_price_ceiling": 18.00,
            "inventory_runway_days": 41,
            "weekly_consumption_units": 900,
            "safety_stock_days": 14,
            "open_supplier_quotes": 5,
            "market_index_trend": "flat QoQ",
        },
    },
    "suppliers": {
        "v_cheap_supplies": {
            "display_name": "V-Cheap Supplies",
            "reliability_score": 0.65,
            "on_time_delivery_rate": 0.71,
            "quality_defect_ppm": 950,
            "spend_ytd_usd": 1_240_000,
            "contract_status": "renewal_window_open",
            "historical_incidents": [
                "3 late deliveries in last 6 POs",
                "1 quality batch rejection (Q2)",
            ],
        },
        "v_prime_components": {
            "display_name": "Prime Components Ltd.",
            "reliability_score": 0.94,
            "on_time_delivery_rate": 0.98,
            "quality_defect_ppm": 120,
            "spend_ytd_usd": 3_850_000,
            "contract_status": "active_until_2027_Q2",
            "historical_incidents": [],
        },
        "v_apex_industrial": {
            "display_name": "Apex Industrial",
            "reliability_score": 0.82,
            "on_time_delivery_rate": 0.88,
            "quality_defect_ppm": 340,
            "spend_ytd_usd": 960_000,
            "contract_status": "trial_period",
            "historical_incidents": ["1 documentation non-conformance (Q1)"],
        },
    },
    "mandates": {
        "CMP-4410": {
            "max_price_ceiling": 4.25,
            "target_unit_price": 3.95,
            "preferred_payment_terms": "Net-60",
            "walk_away_conditions": [
                "price above ceiling",
                "payment terms tighter than Net-30",
                "no delivery guarantee before runway exhaustion",
            ],
        },
        "CMP-2210": {
            "max_price_ceiling": 18.00,
            "target_unit_price": 17.10,
            "preferred_payment_terms": "Net-45",
            "walk_away_conditions": [
                "price above ceiling",
                "MOQ above 2,000 units",
            ],
        },
    },
}


def _unknown(kind: str, identifier: str) -> dict[str, Any]:
    return {
        "error": f"unknown_{kind}",
        "message": f"No {kind} record for '{identifier}'.",
        "known_ids": sorted(
            MOCK_ERP[
                "components" if kind == "component" else "suppliers"
            ].keys()
        ),
    }


# --------------------------------------------------------------------------
# ERP tools (Strategist)
# --------------------------------------------------------------------------


def get_component_baseline(component_id: str) -> dict[str, Any]:
    """Fetch commercial baseline pricing for a component from the ERP.

    Args:
        component_id: ERP component identifier, e.g. "CMP-4410".

    Returns:
        dict with baseline_unit_price, target_unit_price,
        open_supplier_quotes and market_index_trend for the component.
    """
    component = MOCK_ERP["components"].get(component_id)
    if component is None:
        return _unknown("component", component_id)
    return {
        "component_id": component_id,
        "name": component["name"],
        "baseline_unit_price": component["baseline_unit_price"],
        "target_unit_price": component["target_unit_price"],
        "open_supplier_quotes": component["open_supplier_quotes"],
        "market_index_trend": component["market_index_trend"],
    }


def get_supplier_reliability(supplier_id: str) -> dict[str, Any]:
    """Fetch the internal scorecard for a supplier.

    Args:
        supplier_id: Supplier identifier, e.g. "v_cheap_supplies".

    Returns:
        dict with reliability_score, on_time_delivery_rate, quality_defect_ppm,
        spend_ytd_usd, contract_status and historical_incidents. The
        supplier's own performance numbers may be cited back to them during
        negotiation.
    """
    supplier = MOCK_ERP["suppliers"].get(supplier_id)
    if supplier is None:
        return _unknown("supplier", supplier_id)
    return {"supplier_id": supplier_id, **supplier}


def get_inventory_runway(component_id: str) -> dict[str, Any]:
    """Fetch INTERNAL inventory urgency data for a component.

    Args:
        component_id: ERP component identifier, e.g. "CMP-4410".

    Returns:
        dict with inventory_runway_days, weekly_consumption_units and
        safety_stock_days. STRICTLY INTERNAL — never communicate these
        numbers to the vendor.
    """
    component = MOCK_ERP["components"].get(component_id)
    if component is None:
        return _unknown("component", component_id)
    return {
        "component_id": component_id,
        "inventory_runway_days": component["inventory_runway_days"],
        "weekly_consumption_units": component["weekly_consumption_units"],
        "safety_stock_days": component["safety_stock_days"],
        "classification": "INTERNAL — do not disclose to vendor",
    }


def get_negotiation_mandate(component_id: str) -> dict[str, Any]:
    """Fetch the INTERNAL sourcing mandate (authorization limits) for a component.

    Args:
        component_id: ERP component identifier, e.g. "CMP-4410".

    Returns:
        dict with max_price_ceiling, target_unit_price,
        preferred_payment_terms and walk_away_conditions. STRICTLY INTERNAL —
        the ceiling must never appear in any vendor-facing message.
    """
    mandate = MOCK_ERP["mandates"].get(component_id)
    if mandate is None:
        return _unknown("component", component_id)
    return {
        "component_id": component_id,
        **mandate,
        "classification": "INTERNAL — do not disclose to vendor",
    }


# --------------------------------------------------------------------------
# Explainability tool (Auditor)
# --------------------------------------------------------------------------


def scan_for_data_leaks(message: str) -> dict[str, Any]:
    """Scan a drafted vendor message for internal-data leaks before you
    approve it. This is the deterministic compliance firewall.

    Args:
        message: The drafted vendor-facing text to scan.

    Returns:
        dict with `clean` (bool) and `findings` (list of category, severity,
        excerpt). A finding with severity "critical" means the message must
        be rejected regardless of tone or intent.
    """
    return guard_message(message).to_dict()
