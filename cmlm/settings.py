"""Load and validate config.yaml, so mistakes fail fast with a readable message."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

LM_STATUSES = {"Draft", "New", "Proposed", "Approved", "InProgress", "OnHold", "Declined", "Completed"}
LM_PRIORITIES = {"None", "Low", "Medium", "High"}
CM_STATUSES = {"not started", "in progress", "review", "completed", "not applicable"}
ON_REMOVED = {"decline", "delete", "ignore"}
REGIONS = {"us", "eu", "ca", "au"}

DEFAULT_STATUS_MAP = {
    "Not Started": "Proposed",
    "In Progress": "InProgress",
    "Review": "InProgress",
    "Completed": "Completed",
}
DEFAULT_PRIORITY_MAP = {"Critical": "High", "High": "High", "Medium": "Medium", "Low": "Low"}


class ConfigError(Exception):
    pass


@dataclass
class ClientSelector:
    """One configured client: a ControlMap name, optionally pinned to a different LM client."""

    controlmap_name: str
    lifecycle_manager_id: str | None = None


@dataclass
class MappingSettings:
    status: dict[str, str] = field(default_factory=lambda: _lower_keys(DEFAULT_STATUS_MAP))
    priority: dict[str, str] = field(default_factory=lambda: _lower_keys(DEFAULT_PRIORITY_MAP))
    skip_statuses: set[str] = field(default_factory=lambda: {"not applicable"})
    default_status: str = "Proposed"


@dataclass
class Settings:
    region: str = "us"
    sync_all: bool = False
    clients: list[ClientSelector] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)
    on_removed: str = "decline"
    mapping: MappingSettings = field(default_factory=MappingSettings)
    assign_users: bool = True
    assignee_overrides: dict[str, str] = field(default_factory=dict)


def load(path: Path) -> Settings:
    if not path.exists():
        raise ConfigError(f"{path} not found")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse(raw)


def parse(raw: dict) -> Settings:
    s = Settings()

    s.region = str(raw.get("region", "us")).lower()
    _check(s.region in REGIONS, f"region must be one of {sorted(REGIONS)}, got {s.region!r}")

    clients = raw.get("clients", [])
    if isinstance(clients, str):
        _check(clients.lower() == "all", f"clients must be 'all' or a list of client names, got {clients!r}")
        s.sync_all = True
    else:
        for entry in clients or []:
            if isinstance(entry, str):
                s.clients.append(ClientSelector(entry))
            elif isinstance(entry, dict) and entry.get("name"):
                if entry.get("enabled", True):
                    s.clients.append(ClientSelector(entry["name"], entry.get("lifecycle_manager_id")))
            else:
                raise ConfigError(f"Each client must be a name or have a 'name' field, got {entry!r}")
    s.exclude = {str(n).lower() for n in raw.get("exclude") or []}

    s.on_removed = str(raw.get("on_removed", "decline")).lower()
    _check(s.on_removed in ON_REMOVED, f"on_removed must be one of {sorted(ON_REMOVED)}, got {s.on_removed!r}")

    m = raw.get("mapping") or {}
    if "status" in m:
        s.mapping.status = _validated_map(m["status"], LM_STATUSES, "mapping.status")
    if "priority" in m:
        s.mapping.priority = _validated_map(m["priority"], LM_PRIORITIES, "mapping.priority")
    if "skip_statuses" in m:
        s.mapping.skip_statuses = {str(x).lower() for x in m["skip_statuses"] or []}
    a = raw.get("assignees") or {}
    _check(isinstance(a, dict), "assignees must be a section with 'enabled' and/or 'overrides'")
    s.assign_users = bool(a.get("enabled", True))
    overrides = a.get("overrides") or {}
    _check(isinstance(overrides, dict), "assignees.overrides must map a ControlMap email or name to a Lifecycle Manager email")
    s.assignee_overrides = {str(k): str(v) for k, v in overrides.items()}

    for status in s.mapping.status.keys() | s.mapping.skip_statuses:
        _check(status in CM_STATUSES, f"Unknown ControlMap status {status!r}; expected one of {sorted(CM_STATUSES)}")
    return s


def _validated_map(raw: dict, allowed: set[str], where: str) -> dict[str, str]:
    _check(isinstance(raw, dict), f"{where} must be a mapping of ControlMap value: Lifecycle Manager value")
    out = {}
    for cm_value, lm_value in raw.items():
        lm_value = str(lm_value)
        _check(lm_value in allowed, f"{where}: {lm_value!r} is not valid; use one of {sorted(allowed)}")
        out[str(cm_value).strip().lower()] = lm_value
    return out


def _lower_keys(d: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in d.items()}


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ConfigError(message)
