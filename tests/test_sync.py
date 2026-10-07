import json
from datetime import date

import pytest

from cmlm import clients, mapping, settings
from cmlm.clients import ClientPair
from cmlm.engine import Syncer

LM_CLIENT = "lm-1"
PAIR = ClientPair("cm-1", "Contoso", LM_CLIENT, "Contoso Ltd")


def item(**overrides):
    base = {
        "id": 1,
        "code": "AI-1",
        "status": "Not Started",
        "weakness_name": "MFA not enforced",
        "weakness_description": "Admins can sign in without MFA.",
        "corrective_action": "Enforce conditional access.",
        "priority": "Critical",
        "planned_end_date": "2026-11-15T00:00:00Z",
        "effort_in_hours": 6,
        "cost": 1250.5,
        "currency": "USD",
        "created_at": "2026-09-01T00:00:00Z",
    }
    return {**base, **overrides}


class FakeCM:
    def __init__(self, items):
        self.items = items

    def action_items(self, client_id):
        return self.items


class FakeLM:
    def __init__(self, initiatives=None):
        self.store = {i["id"]: i for i in (initiatives or [])}
        self.calls = []

    def initiatives(self, client_id):
        return list(self.store.values())

    def create(self, client_id, name, summary_json):
        new_id = f"init-{len(self.store) + 1}"
        self.store[new_id] = {"id": new_id, "name": name, "budget": {"line_items": [], "currency": {"code_alpha": "USD"}}}
        self.calls.append(("create", name))
        return new_id

    def patch(self, initiative_id, **fields):
        self.calls.append(("patch", initiative_id, sorted(fields)))

    def set_status(self, initiative_id, status):
        self.calls.append(("status", initiative_id, status))

    def set_priority(self, initiative_id, priority):
        self.calls.append(("priority", initiative_id, priority))

    def set_quarter(self, initiative_id, q):
        self.calls.append(("schedule", initiative_id, q))

    def set_budget(self, initiative_id, lines):
        self.calls.append(("budget", initiative_id, lines))

    def delete(self, initiative_id):
        self.store.pop(initiative_id, None)
        self.calls.append(("delete", initiative_id))


def run(items, lm, state=None, config=None):
    state = state if state is not None else {}
    syncer = Syncer(FakeCM(items), lm, state, settings.parse(config or {}))
    syncer.sync_client(PAIR)
    return syncer, state


# --- mapping -------------------------------------------------------------


def test_spec_maps_fields():
    spec = mapping.build_spec(item())
    assert spec.name == "AI-1 · MFA not enforced"
    assert spec.status == "Proposed"
    assert spec.priority == "High"
    assert spec.fiscal_quarter == {"year": 2026, "quarter": 4}
    assert spec.estimated_hours == 6
    assert spec.budget_line == {"label": "ControlMap AI-1 remediation", "cost_subunits": 125050, "cost_type": "Fixed"}
    doc = json.loads(spec.summary_json)
    assert doc["type"] == "doc"
    assert "Admins can sign in without MFA." in json.dumps(doc)


def test_hours_accepts_documented_field_name_too():
    i = item(effort_in_hours=None, efforts_in_hours=25)
    assert mapping.build_spec(i).estimated_hours == 25


def test_status_and_priority_mapping():
    assert mapping.build_spec(item(status="Review")).status == "InProgress"
    assert mapping.build_spec(item(status="Completed")).status == "Completed"
    assert mapping.build_spec(item(priority="Medium")).priority == "Medium"
    assert mapping.build_spec(item(priority=None)).priority == "None"


def test_quarter_falls_back_to_roadmap_horizon():
    i = item(planned_end_date=None, roadmap="6 months", created_at="2026-10-07T00:00:00Z")
    assert mapping.target_quarter(i, date(2026, 10, 7)) == {"year": 2027, "quarter": 2}


def test_code_round_trips_through_name():
    assert mapping.code_from_initiative_name(mapping.initiative_name(item())) == "AI-1"
    assert mapping.code_from_initiative_name("Unrelated initiative") is None


# --- engine --------------------------------------------------------------


def test_creates_new_initiative_and_records_state():
    lm = FakeLM()
    syncer, state = run([item()], lm)
    assert syncer.stats["created"] == 1
    assert state["items"]["cm-1:1"]["initiative_id"] == "init-1"
    assert ("status", "init-1", "Proposed") in lm.calls


def test_second_run_is_a_no_op():
    lm = FakeLM()
    _, state = run([item()], lm)
    lm.calls.clear()
    syncer, _ = run([item()], lm, state)
    assert syncer.stats["unchanged"] == 1
    assert lm.calls == []


def test_change_in_controlmap_updates_initiative():
    lm = FakeLM()
    _, state = run([item()], lm)
    lm.calls.clear()
    syncer, _ = run([item(status="In Progress")], lm, state)
    assert syncer.stats["updated"] == 1
    assert ("status", "init-1", "InProgress") in lm.calls


def test_not_applicable_is_skipped():
    lm = FakeLM()
    syncer, _ = run([item(status="Not Applicable")], lm)
    assert syncer.stats["skipped"] == 1
    assert lm.calls == []


def test_previously_synced_item_marked_not_applicable_is_declined():
    lm = FakeLM()
    _, state = run([item()], lm)
    syncer, _ = run([item(status="Not Applicable")], lm, state)
    assert syncer.stats["declined"] == 1
    assert lm.calls[-1] == ("status", "init-1", "Declined")


def test_deleted_action_item_is_declined_once():
    lm = FakeLM()
    _, state = run([item()], lm)
    syncer, state = run([], lm, state)
    assert syncer.stats["declined"] == 1
    lm.calls.clear()
    syncer, _ = run([], lm, state)
    assert syncer.stats["declined"] == 0 and lm.calls == []


def test_adopts_existing_initiative_when_state_is_lost():
    lm = FakeLM([{"id": "x9", "name": "AI-1 · old title", "budget": {"line_items": []}}])
    syncer, state = run([item()], lm)
    assert syncer.stats["created"] == 0
    assert state["items"]["cm-1:1"]["initiative_id"] == "x9"


def test_budget_keeps_manual_lines():
    manual = {"label": "Hardware", "cost_subunits": 5000, "cost_type": "Fixed"}
    lm = FakeLM([{"id": "x9", "name": "AI-1 · t", "budget": {"line_items": [manual], "currency": {"code_alpha": "USD"}}}])
    run([item()], lm)
    budget = [c for c in lm.calls if c[0] == "budget"][-1][2]
    assert manual in budget
    assert any(li["label"] == "ControlMap AI-1 remediation" for li in budget)


def test_failure_after_create_is_retried_as_update_not_duplicate():
    class FailingScheduleLM(FakeLM):
        fail = True

        def set_quarter(self, initiative_id, q):
            if self.fail:
                raise RuntimeError("HTTP 422")
            super().set_quarter(initiative_id, q)

    lm = FailingScheduleLM()
    syncer, state = run([item()], lm)
    assert syncer.errors and state["items"]["cm-1:1"]["initiative_id"] == "init-1"
    # Steps after the failed one still ran.
    assert any(c[0] == "budget" for c in lm.calls)
    assert state["items"]["cm-1:1"]["fingerprint"] is None

    lm.fail = False
    syncer, state = run([item()], lm, state)
    assert syncer.stats["created"] == 0 and syncer.stats["updated"] == 1
    assert len(lm.store) == 1
    assert state["items"]["cm-1:1"]["fingerprint"]


def test_quarter_bounds():
    from cmlm.platforms import quarter_bounds

    assert quarter_bounds(2026, 4) == ("2026-10-01T00:00:00Z", "2026-12-31T00:00:00Z")
    assert quarter_bounds(2028, 1) == ("2028-01-01T00:00:00Z", "2028-03-31T00:00:00Z")


def test_does_not_recreate_initiative_deleted_in_lm():
    lm = FakeLM()
    _, state = run([item()], lm)
    lm.store.clear()
    lm.calls.clear()
    syncer, _ = run([item(status="Completed")], lm, state)
    assert syncer.stats["missing_in_lm"] == 1
    assert lm.calls == []


# --- configurable behaviour ----------------------------------------------


def test_on_removed_delete_deletes_and_forgets():
    lm = FakeLM()
    _, state = run([item()], lm, config={"on_removed": "delete"})
    syncer, state = run([], lm, state, config={"on_removed": "delete"})
    assert syncer.stats["deleted"] == 1
    assert ("delete", "init-1") in lm.calls
    assert state["items"] == {}


def test_on_removed_ignore_leaves_initiative_alone():
    lm = FakeLM()
    _, state = run([item()], lm, config={"on_removed": "ignore"})
    lm.calls.clear()
    syncer, _ = run([], lm, state, config={"on_removed": "ignore"})
    assert syncer.stats["left_alone"] == 1
    assert lm.calls == []


def test_custom_status_and_priority_mapping():
    cfg = settings.parse({"mapping": {"status": {"Not Started": "New"}, "priority": {"Critical": "High", "Low": "None"}}})
    assert mapping.build_spec(item(), cfg.mapping).status == "New"
    assert mapping.build_spec(item(priority="Low"), cfg.mapping).priority == "None"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"region": "mars"}, "region"),
        ({"clients": "everyone"}, "clients must be"),
        ({"on_removed": "archive"}, "on_removed"),
        ({"mapping": {"status": {"Not Started": "Open"}}}, "not valid"),
        ({"mapping": {"status": {"Started": "New"}}}, "Unknown ControlMap status"),
    ],
)
def test_config_errors_are_readable(raw, message):
    with pytest.raises(settings.ConfigError, match=message):
        settings.parse(raw)


# --- client matching -----------------------------------------------------

CM_CLIENTS = [
    {"client": {"id": "a", "name": "Alpha"}, "action_summary": {"total": 3}},
    {"client": {"id": "b", "name": "Bravo"}, "action_summary": {"total": 0}},
    {"client": {"id": "c", "name": "Charlie"}, "action_summary": {"total": 5}},
]
LM_CLIENTS = {"a": "Alpha Inc", "b": "Bravo LLC", "z": "Charlie Renamed"}


def test_all_clients_match_by_shared_id_and_respect_exclude():
    cfg = settings.parse({"clients": "all", "exclude": ["bravo"]})
    pairs, problems = clients.resolve(cfg, CM_CLIENTS, LM_CLIENTS)
    assert [(p.controlmap_name, p.lm_client_label) for p in pairs] == [("Alpha", "Alpha Inc")]
    assert problems == ["Charlie: no Lifecycle Manager client with the same ID; pin one with lifecycle_manager_id"]


def test_pinned_lm_id_and_unknown_names():
    cfg = settings.parse({"clients": [{"name": "charlie", "lifecycle_manager_id": "z"}, "Delta"]})
    pairs, problems = clients.resolve(cfg, CM_CLIENTS, LM_CLIENTS)
    assert [(p.controlmap_id, p.lm_client_id) for p in pairs] == [("c", "z")]
    assert problems == ["Delta: not found in ControlMap (check the spelling in config.yaml)"]


def test_shipped_config_is_valid():
    from pathlib import Path

    cfg = settings.load(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg.on_removed == "decline" and cfg.clients
