"""Typed contracts for inter-agent handoffs.

Every boundary between agents in this system is a typed contract:

* ``Strategist -> Drafter``: a :class:`StrategyDirective` (machine-readable
  negotiation directive).
* ``Auditor -> Orchestrator``: an :class:`AuditVerdict` (approve / reject /
  escalate decision with feedback).

Model output is untrusted text until it survives :func:`extract_json` and
Pydantic validation. Parsing is *lenient at the edges* (fenced JSON, prose
surrounding the object, string-typed numbers) but *strict about semantics*
(a strategy without instructions, or a verdict without a status, is a
contract violation and is reported as such).

Fail-safe policy: an unparseable audit verdict is treated as ``rejected``.
A pipeline that cannot prove compliance must not ship the message.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# Strategy contract (Strategist -> Drafter)
# --------------------------------------------------------------------------

#: Canonical negotiation tactic identifiers. Prompts ask for these exact
#: slugs; :func:`StrategyDirective` normalizes human spellings onto them.
CANONICAL_TACTICS: tuple[str, ...] = (
    "anchor_low",
    "call_bluff",
    "pivot_to_payment_terms",
    "volume_leverage",
    "accept",
    "hold_firm",
)

_TACTIC_ALIASES: dict[str, str] = {
    "anchor low": "anchor_low",
    "anchor_low": "anchor_low",
    "call bluff": "call_bluff",
    "call_bluff": "call_bluff",
    "bluff": "call_bluff",
    "pivot to payment terms": "pivot_to_payment_terms",
    "pivot_to_payment_terms": "pivot_to_payment_terms",
    "pivot to terms": "pivot_to_payment_terms",
    "volume leverage": "volume_leverage",
    "volume_leverage": "volume_leverage",
    "accept": "accept",
    "accept offer": "accept",
    "hold firm": "hold_firm",
    "hold_firm": "hold_firm",
    "walk away": "hold_firm",
    "walk_away": "hold_firm",
}


class StrategyDirective(BaseModel):
    """The strict JSON directive produced by the Strategist agent.

    It is deliberately *internal*: it may reference private metrics (ceiling,
    runway). It is the Drafter's job to communicate the tactic without
    revealing the numbers behind it.
    """

    analysis: str = Field(
        description="Short internal assessment of the vendor's demands and our leverage."
    )
    selected_tactic: str = Field(
        description="Canonical tactic slug, e.g. 'call_bluff'."
    )
    target_price: float | None = Field(
        default=None,
        description="The unit price we intend to close at.",
    )
    max_concession: float | None = Field(
        default=None,
        description="Internal walk-away price. NEVER communicate this to the vendor.",
    )
    instructions_for_drafter: list[str] = Field(
        min_length=1,
        description="Ordered, concrete instructions the Drafter must follow.",
    )
    tool_evidence: list[str] = Field(
        default_factory=list,
        description="Names of the ERP tool queries this directive is based on.",
    )

    @field_validator("selected_tactic", mode="before")
    @classmethod
    def _normalize_tactic(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("selected_tactic must be a non-empty string")
        raw = value.strip().lower()
        if raw in _TACTIC_ALIASES:
            return _TACTIC_ALIASES[raw]
        # Unknown tactic: keep a normalized slug rather than failing the
        # whole contract — tactics are an open vocabulary in practice.
        return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")

    @field_validator("target_price", "max_concession")
    @classmethod
    def _positive_price(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("prices must be positive")
        return value

    @field_validator("instructions_for_drafter", mode="before")
    @classmethod
    def _drop_blank_instructions(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("instructions_for_drafter must be a list of strings")
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("instructions_for_drafter must not be empty")
        return cleaned

    @field_validator("analysis", mode="before")
    @classmethod
    def _analysis_must_exist(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("analysis must not be empty")
        return text


# --------------------------------------------------------------------------
# Audit contract (Auditor -> Orchestrator)
# --------------------------------------------------------------------------

AuditStatus = Literal["approved", "rejected", "escalated"]


class AuditVerdict(BaseModel):
    """The compliance decision produced by the Auditor agent (or by the
    deterministic guard rail when it overrides the Auditor)."""

    status: AuditStatus = Field(
        description="approved | rejected | escalated"
    )
    feedback: str = Field(
        default="",
        description="For rejected verdicts: exactly what the Drafter must fix.",
    )
    final_message: str | None = Field(
        default=None,
        description="For approved verdicts: the exact text that may be sent.",
    )
    violations: list[str] = Field(
        default_factory=list,
        description="Human-readable list of policy violations found.",
    )


# --------------------------------------------------------------------------
# Robust JSON extraction
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _find_balanced_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, string-aware."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def extract_json(text: Any) -> dict[str, Any]:
    """Extract the first JSON object from untrusted model output.

    Handles: fenced ```json blocks, prose before/after the object, and
    already-parsed dicts (returned as-is). Raises ``ValueError`` when no
    parseable object exists — callers decide the fail-safe.

    Args:
        text: raw model output (str) or an already-decoded mapping.

    Returns:
        The decoded JSON object.

    Raises:
        ValueError: if no JSON object can be extracted and decoded.
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty model output: no JSON object found")

    candidates: list[str] = []
    for fenced in _FENCE_RE.findall(text):
        candidates.append(fenced.strip())
    candidates.append(text.strip())

    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass

    balanced = _find_balanced_json_object(text)
    if balanced is not None:
        try:
            decoded = json.loads(balanced)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON object in model output: {exc}") from exc

    raise ValueError("no JSON object found in model output")


def _coerce_numbers(payload: dict[str, Any], *keys: str) -> None:
    """Best-effort coercion of numeric fields written as strings."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            stripped = value.strip().lstrip("$").replace(",", "")
            try:
                payload[key] = float(stripped)
            except ValueError:
                payload[key] = None


def parse_strategy(text: Any) -> StrategyDirective:
    """Parse and validate a :class:`StrategyDirective` from model output.

    Raises:
        ValueError: with an actionable message when the contract is broken.
    """
    payload = extract_json(text)
    _coerce_numbers(payload, "target_price", "max_concession")
    try:
        return StrategyDirective.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError hierarchy
        raise ValueError(f"StrategyDirective contract violation: {exc}") from exc


def parse_verdict(text: Any) -> AuditVerdict:
    """Parse and validate an :class:`AuditVerdict` from model output.

    Raises:
        ValueError: when the verdict contract is broken.
    """
    payload = extract_json(text)
    try:
        return AuditVerdict.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"AuditVerdict contract violation: {exc}") from exc


#: Violation markers emitted by :func:`parse_verdict_failsafe` when the
#: Auditor's raw output broke its own contract. Callers use these to decide
#: whether the model's output must be replaced with the canonical verdict.
CONTRACT_VIOLATION_MARKERS: tuple[str, ...] = (
    "verdict_contract_violation",
    "missing_final_message",
)


def parse_verdict_failsafe(text: Any) -> AuditVerdict:
    """Parse a verdict, failing *closed*: anything unparseable is a rejection.

    A generator-critic loop must never treat garbage as approval. Contract
    failures are tagged with markers from :data:`CONTRACT_VIOLATION_MARKERS`.
    """
    try:
        verdict = parse_verdict(text)
    except ValueError as exc:
        return AuditVerdict(
            status="rejected",
            feedback=(
                "Verdict was not a valid AuditVerdict JSON object "
                f"({exc}). Re-draft conservatively and re-submit."
            ),
            violations=["verdict_contract_violation"],
        )
    if verdict.status == "approved" and not (verdict.final_message or "").strip():
        return AuditVerdict(
            status="rejected",
            feedback=(
                "Approved verdicts must carry `final_message`. "
                "Re-submit the verdict with the exact approved text."
            ),
            violations=["missing_final_message"],
        )
    return verdict


def canonical_json(model: BaseModel) -> str:
    """Deterministic, sorted-key JSON serialization for state storage and
    tool payloads."""
    return json.dumps(model.model_dump(), sort_keys=True, ensure_ascii=False)
