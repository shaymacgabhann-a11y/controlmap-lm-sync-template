"""Work out which ControlMap client syncs to which Lifecycle Manager client.

ControlMap and Lifecycle Manager share ScalePad client IDs, so a ControlMap client
matches the LM client with the same ID. A config entry can pin a different LM client
when that isn't the case.
"""

from __future__ import annotations

from dataclasses import dataclass

from .settings import Settings


@dataclass
class ClientPair:
    controlmap_id: str
    controlmap_name: str
    lm_client_id: str
    lm_client_label: str


@dataclass
class ClientRow:
    """One line of the list-clients report."""

    controlmap_id: str
    controlmap_name: str
    action_items: int | None
    lm_client_id: str | None
    lm_client_label: str | None
    selected: bool
    note: str = ""


def resolve(settings: Settings, cm_clients: list[dict], lm_clients: dict[str, str]) -> tuple[list[ClientPair], list[str]]:
    """Return (pairs to sync, problems to report)."""
    rows = report(settings, cm_clients, lm_clients)
    pairs = [
        ClientPair(r.controlmap_id, r.controlmap_name, r.lm_client_id, r.lm_client_label or "")
        for r in rows
        if r.selected and r.lm_client_id
    ]
    problems = [f"{r.controlmap_name}: {r.note}" for r in rows if r.selected and not r.lm_client_id]

    known = {r.controlmap_name.lower() for r in rows}
    problems += [
        f"{sel.controlmap_name}: not found in ControlMap (check the spelling in config.yaml)"
        for sel in settings.clients
        if sel.controlmap_name.lower() not in known
    ]
    return pairs, problems


def report(settings: Settings, cm_clients: list[dict], lm_clients: dict[str, str]) -> list[ClientRow]:
    pinned = {sel.controlmap_name.lower(): sel for sel in settings.clients}
    rows = []
    for summary in cm_clients:
        cm = summary["client"]
        name_key = cm["name"].lower()
        sel = pinned.get(name_key)

        if settings.sync_all:
            selected = name_key not in settings.exclude
        else:
            selected = sel is not None

        lm_id = (sel.lifecycle_manager_id if sel and sel.lifecycle_manager_id else None) or cm["id"]
        note = ""
        if lm_id not in lm_clients:
            note = (
                f"pinned Lifecycle Manager ID {lm_id} not found"
                if sel and sel.lifecycle_manager_id
                else "no Lifecycle Manager client with the same ID; pin one with lifecycle_manager_id"
            )
            lm_id = None

        rows.append(
            ClientRow(
                controlmap_id=cm["id"],
                controlmap_name=cm["name"],
                action_items=(summary.get("action_summary") or {}).get("total"),
                lm_client_id=lm_id,
                lm_client_label=lm_clients.get(lm_id) if lm_id else None,
                selected=selected,
                note=note,
            )
        )
    return sorted(rows, key=lambda r: r.controlmap_name.lower())
