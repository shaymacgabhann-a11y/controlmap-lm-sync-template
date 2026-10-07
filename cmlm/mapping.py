"""Pure translation from a ControlMap Action Item to the Initiative fields we manage.

Nothing in here touches the network, so it is the part covered most by tests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from .settings import MappingSettings

NAME_SEPARATOR = " · "
MAX_NAME_LENGTH = 200

ROADMAP_MONTHS = {"3 months": 3, "6 months": 6, "12 months": 12}


@dataclass
class InitiativeSpec:
    """Everything the sync writes onto an Initiative for one Action Item."""

    code: str
    name: str
    summary_json: str
    status: str
    priority: str
    fiscal_quarter: dict | None
    estimated_hours: float | None
    budget_line: dict | None = field(default=None)
    budget_currency: str | None = field(default=None)

    def fingerprint(self) -> str:
        """Stable hash of the spec; a change means ControlMap changed since the last sync."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def is_skipped(item: dict, settings: MappingSettings | None = None) -> bool:
    settings = settings or MappingSettings()
    return _norm(item.get("status")) in settings.skip_statuses


def item_title(item: dict) -> str:
    # The search endpoint's documented schema omits the title; create/patch call it
    # weakness_name. Accept the plausible variants so a docs/API mismatch doesn't break us.
    for key in ("weakness_name", "title", "name"):
        if item.get(key):
            return str(item[key]).strip()
    return "Untitled Action Item"


def initiative_name(item: dict) -> str:
    name = f"{item['code']}{NAME_SEPARATOR}{item_title(item)}"
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1] + "…"


def code_from_initiative_name(name: str) -> str | None:
    """Recover the ControlMap code (e.g. "AI-12") from an Initiative name we created."""
    if NAME_SEPARATOR not in name:
        return None
    return name.split(NAME_SEPARATOR, 1)[0].strip() or None


def build_spec(item: dict, settings: MappingSettings | None = None, *, today: date | None = None) -> InitiativeSpec:
    settings = settings or MappingSettings()
    today = today or datetime.now(timezone.utc).date()
    cost = _number(item.get("cost"))
    budget_line = None
    if cost:
        budget_line = {
            "label": budget_label(item["code"]),
            "cost_subunits": round(cost * 100),
            "cost_type": "Fixed",
        }
    return InitiativeSpec(
        code=item["code"],
        name=initiative_name(item),
        summary_json=json.dumps(summary_doc(item), separators=(",", ":")),
        status=settings.status.get(_norm(item.get("status")), settings.default_status),
        priority=settings.priority.get(_norm(item.get("priority")), "None"),
        fiscal_quarter=target_quarter(item, today),
        estimated_hours=_hours(item),
        budget_line=budget_line,
        budget_currency=(item.get("currency") or None) if budget_line else None,
    )


def budget_label(code: str) -> str:
    return f"ControlMap {code} remediation"


def target_quarter(item: dict, today: date) -> dict | None:
    """Pick the roadmap quarter: planned completion date, else due date, else start date, else roadmap horizon."""
    # ControlMap returns planned_completion_date; its create docs call it planned_end_date.
    for key in ("planned_completion_date", "planned_end_date", "due_date", "planned_start_date"):
        d = _date(item.get(key))
        if d:
            return _quarter(d)
    months = ROADMAP_MONTHS.get(_norm(item.get("roadmap")))
    if months:
        base = _date(item.get("created_at")) or today
        month_index = base.month - 1 + months
        return _quarter(date(base.year + month_index // 12, month_index % 12 + 1, 1))
    return None


def summary_doc(item: dict) -> dict:
    """ProseMirror document for the Initiative's executive summary."""
    content: list[dict] = []
    for heading, key in (
        ("Weakness", "weakness_description"),
        ("Corrective action", "corrective_action"),
        ("Implementation notes", "implementation_notes"),
    ):
        text = (item.get(key) or "").strip()
        if text:
            content.append(_paragraph(heading, bold=True))
            content.extend(_paragraph(line) for line in text.splitlines() if line.strip())
    content.append(
        _paragraph(f"Synced from ControlMap Action Item {item['code']}. Edits to these fields are overwritten when the Action Item changes.", italic=True)
    )
    return {"type": "doc", "content": content}


def _paragraph(text: str, *, bold: bool = False, italic: bool = False) -> dict:
    node: dict = {"type": "text", "text": text}
    marks = [{"type": "bold"}] * bold + [{"type": "italic"}] * italic
    if marks:
        node["marks"] = marks
    return {"type": "paragraph", "content": [node]}


def _hours(item: dict) -> float | None:
    # The search API returns effort_in_hours; the create/patch docs call it efforts_in_hours.
    for key in ("effort_in_hours", "efforts_in_hours", "efforts"):
        hours = _number(item.get(key))
        if hours:
            return hours
    return None


def _quarter(d: date) -> dict:
    return {"year": d.year, "quarter": (d.month - 1) // 3 + 1}


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _number(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None
