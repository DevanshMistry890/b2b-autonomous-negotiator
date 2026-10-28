"""Deterministic compliance firewall — model-independent leak detection.

The LLM Auditor is *advisory*: it can be prompt-injected, fooled, or simply
wrong. This module is *authoritative*: it is plain Python over the draft that
is about to leave the system. Both layers run, and when they disagree the
deterministic layer wins (see ``loop_control.resolve_audit`` and
``loop_control.enforce_output_firewall``).

Design notes:

* **Blocking vs. advisory findings.** ``critical`` findings block the
  message; ``warning`` findings are recorded and surfaced to the Auditor but
  do not veto on their own. This keeps the false-positive rate low enough
  for a negotiation to stay fluent while hard-blocking genuine leaks
  (price ceilings, inventory runway, internal state keys).
* **Explainability.** Every finding carries its category and matched
  excerpt, so rejection feedback can quote the exact leak — which is also
  what the offline eval harness scores against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CRITICAL = "critical"
WARNING = "warning"

#: Category names used by findings and by the eval harness.
CATEGORY_PRICE_CEILING = "price_ceiling"
CATEGORY_INTERNAL_METRIC = "internal_metric"
CATEGORY_STATE_KEY = "state_key_leak"
CATEGORY_MARGIN = "margin_language"
CATEGORY_EMAIL_FORMAT = "email_format"


@dataclass(frozen=True)
class LeakFinding:
    """A single detected leak in an outgoing message."""

    category: str
    severity: str
    excerpt: str
    start: int
    end: int

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "severity": self.severity,
            "excerpt": self.excerpt,
            "span": [self.start, self.end],
        }


@dataclass
class GuardReport:
    """Result of scanning one message with the deterministic firewall."""

    findings: list[LeakFinding] = field(default_factory=list)

    @property
    def blocking_findings(self) -> list[LeakFinding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def clean(self) -> bool:
        """True when nothing in the message blocks transmission."""
        return not self.blocking_findings

    def summary(self) -> str:
        if not self.findings:
            return "Deterministic firewall: clean — no internal data detected."
        lines = ["Deterministic firewall findings:"]
        for finding in self.findings:
            lines.append(
                f"- [{finding.severity}/{finding.category}] "
                f'"{finding.excerpt}"'
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# Pattern library
# --------------------------------------------------------------------------

_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        CATEGORY_PRICE_CEILING,
        CRITICAL,
        re.compile(
            r"(?i)\b("
            r"max(?:imum)?[_\s-]*price[_\s-]*ceiling"
            r"|price[_\s-]*ceiling"
            r"|ceiling"
            r"|walk[_\s-]?away[_\s-]+(?:price|point|is)"
            r"|reservation[_\s-]*price"
            r"|absolute[_\s-]*max(?:imum)?(?=[\s,.;:]|$)"
            r"|max(?:imum)?\s+(?:we\s+can|offer|bid|pay)"
            r")"
        ),
    ),
    (
        CATEGORY_INTERNAL_METRIC,
        CRITICAL,
        re.compile(
            r"(?i)\b("
            r"runway"
            r"|inventory[_\s-]*runway"
            r"|days?\s+of[_\s-]+(?:inventory|stock)"
            r"|inventory[_\s-]*(?:left|remaining|position|cover(?:age)?)"
            r"|burn[_\s-]*rate"
            r"|margin[_\s-]*impact"
            r"|margin[_\s-]*(?:erosion|pressure|calculation|analysis)"
            r"|cost[_\s-]*basis"
            r"|our\s+(?:unit\s+)?cost"
            r"|internal[_\s-]*(?:analysis|metrics?|calculations?|mandate)"
            r"|safety[_\s-]*stock"
            r")"
        ),
    ),
    (
        CATEGORY_STATE_KEY,
        CRITICAL,
        re.compile(
            r"\b("
            r"max_price_ceiling"
            r"|inventory_runway"
            r"|selected_tactic"
            r"|max_concession"
            r"|instructions_for_drafter"
            r"|previous_feedback"
            r"|audit_result"
            r"|current_draft"
            r"|strategy_directive"
            r")\b"
        ),
    ),
    (
        CATEGORY_MARGIN,
        WARNING,
        re.compile(r"(?i)\b(margins?|markups?|cost-plus)\b"),
    ),
    (
        CATEGORY_EMAIL_FORMAT,
        WARNING,
        re.compile(
            r"(?i)(^\s*subject\s*:"
            r"|\bdear\s+[A-Z]"
            r"|\b(sincerely|best\s+regards|kind\s+regards|warm\s+regards)\b)"
        ),
    ),
)


def scan_text(text: str) -> list[LeakFinding]:
    """Scan a message and return every leak finding (critical and warning).

    Args:
        text: the exact outgoing message text.

    Returns:
        Findings in order of first occurrence, with match spans.
    """
    findings: list[LeakFinding] = []
    if not text:
        return findings
    for category, severity, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                LeakFinding(
                    category=category,
                    severity=severity,
                    excerpt=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
    findings.sort(key=lambda f: (f.start, f.category))
    return findings


def guard_message(text: str) -> GuardReport:
    """Run the deterministic firewall over one message.

    Args:
        text: the exact outgoing message text.

    Returns:
        A GuardReport whose ``clean`` flag is the transmission verdict.
    """
    return GuardReport(findings=scan_text(text))


#: The only message the pipeline may emit when it cannot prove compliance:
#: no numbers, no positions, no internal vocabulary.
SAFE_FALLBACK_MESSAGE = (
    "Thanks for the update. We are reviewing your proposal internally and "
    "will come back to you with our position shortly."
)
