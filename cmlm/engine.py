"""Reconcile one client's ControlMap Action Items with its Lifecycle Manager Initiatives."""

from __future__ import annotations

import logging
from collections import Counter

from . import mapping
from .clients import ClientPair
from .platforms import ControlMap, LifecycleManager
from .settings import Settings
from .users import UserDirectory

log = logging.getLogger(__name__)

DECLINED = "Declined"


class StepsFailed(Exception):
    pass


class Syncer:
    def __init__(self, cm: ControlMap, lm: LifecycleManager, state: dict, settings: Settings | None = None,
                 users: UserDirectory | None = None):
        self.cm = cm
        self.lm = lm
        self.settings = settings or Settings()
        self.users = users  # None = don't manage assignees
        self.unmatched_people: dict[str, str] = {}
        # state["items"]: "<cm client id>:<action item id>" -> {initiative_id, fingerprint, code, ...}
        self.items: dict = state.setdefault("items", {})
        self.stats: Counter = Counter()
        self.errors: list[str] = []

    def sync_client(self, pair: ClientPair) -> None:
        cm_id = pair.controlmap_id
        log.info("Syncing ControlMap %r -> Lifecycle Manager %r", pair.controlmap_name, pair.lm_client_label or pair.lm_client_id)

        action_items = self.cm.action_items(cm_id)
        initiatives = {i["id"]: i for i in self.lm.initiatives(pair.lm_client_id)}
        by_code = {}
        for init in initiatives.values():
            code = mapping.code_from_initiative_name(init.get("name", ""))
            if code:
                by_code.setdefault(code, init)

        seen_keys = set()
        for item in action_items:
            key = f"{cm_id}:{item['id']}"
            seen_keys.add(key)
            try:
                self._sync_item(key, item, pair, initiatives, by_code)
            except Exception as exc:  # keep going; one bad item shouldn't stop the run
                self._error(f"{pair.controlmap_name} {item.get('code', item['id'])}: {exc}")

        # Action Items that disappeared from ControlMap.
        for key, entry in list(self.items.items()):
            if not key.startswith(f"{cm_id}:") or key in seen_keys or entry.get("retired"):
                continue
            try:
                self._retire(key, entry, initiatives, reason="deleted in ControlMap")
            except Exception as exc:
                self._error(f"{pair.controlmap_name} {entry.get('code')}: {exc}")

    def _sync_item(self, key, item, pair, initiatives, by_code) -> None:
        entry = self.items.get(key)

        if mapping.is_skipped(item, self.settings.mapping):
            if entry and entry["initiative_id"] in initiatives and not entry.get("retired"):
                self._retire(key, entry, initiatives, reason=f"status is {item.get('status')}")
            else:
                self.stats["skipped"] += 1
            return

        spec = mapping.build_spec(item, self.settings.mapping)
        log.info("%s: ControlMap fields with values: %s", item["code"],
                 ", ".join(sorted(k for k, v in item.items() if v not in (None, "", [], {}))))

        if entry is None:
            existing = by_code.get(item["code"])
            if existing:  # state was lost but the Initiative exists: adopt it rather than duplicate
                log.info("%s: adopting existing Initiative %s", item["code"], existing["id"])
                entry = self.items[key] = {"initiative_id": existing["id"], "code": item["code"], "fingerprint": None}
            else:
                initiative_id = self.lm.create(pair.lm_client_id, spec.name, spec.summary_json)
                log.info("%s: created Initiative %s", item["code"], initiative_id)
                # Record it before the follow-up calls: if one fails, the next run retries
                # them as an update (no fingerprint) instead of creating a duplicate.
                self.items[key] = {"initiative_id": initiative_id, "code": item["code"], "fingerprint": None}
                self.stats["created"] += 1
                self._apply(initiative_id, spec, existing_budget=[], created=True)
                self._record(key, initiative_id, spec, item)
                self._sync_assignee(key, item)
                return

        initiative = initiatives.get(entry["initiative_id"])
        if initiative is None:
            # Someone deleted it in LM on purpose; don't fight them by recreating it.
            log.warning("%s: Initiative %s no longer exists in LM, not recreating", item["code"], entry["initiative_id"])
            self.stats["missing_in_lm"] += 1
            return

        if entry.get("fingerprint") == spec.fingerprint():
            self.stats["unchanged"] += 1
            self._sync_assignee(key, item)
            return

        currency = ((initiative.get("budget") or {}).get("currency") or {}).get("code_alpha")
        self._apply(entry["initiative_id"], spec, existing_budget=_budget_lines(initiative), currency=currency)
        self._record(key, entry["initiative_id"], spec, item)
        log.info("%s: updated Initiative %s", item["code"], entry["initiative_id"])
        self.stats["updated"] += 1
        self._sync_assignee(key, item)

    def _sync_assignee(self, key: str, item: dict) -> None:
        """Assign the Initiative to the LM user matching the ControlMap responsible person.

        Tracked apart from the fingerprint: it's only pushed when the matched user differs
        from the one we last set, so a manual reassignment in LM sticks until the
        responsible person changes in ControlMap.
        """
        if self.users is None:
            return
        person = item.get("responsible_person") or item.get("owner")
        user_id, how = self.users.resolve(person)
        if user_id is None:
            label = (person.get("email") or person.get("name")) if isinstance(person, dict) else person
            if label and label not in self.unmatched_people:
                self.unmatched_people[label] = how
                log.warning("%s: can't assign: %s", item["code"], how)
            self.stats["assignee_unmatched"] += 1
            return
        entry = self.items[key]
        if entry.get("assigned_user_id") == user_id:
            return
        self.lm.set_assignee(entry["initiative_id"], user_id)
        entry["assigned_user_id"] = user_id
        log.info("%s: assigned Initiative %s (matched by %s)", item["code"], entry["initiative_id"], how)
        self.stats["assigned"] += 1

    def _apply(self, initiative_id: str, spec: mapping.InitiativeSpec, existing_budget: list[dict], created=False, currency: str | None = None) -> None:
        """Run every update step even if some fail, then raise one error listing the failures.

        The caller only records the new fingerprint on full success, so failed steps are
        retried on the next run.
        """
        fields = {} if created else {"name": spec.name, "executive_summary_json": spec.summary_json}
        if spec.estimated_hours:
            fields["estimated_hours"] = {"minimum": spec.estimated_hours}

        # Budget PUT replaces every one-time line, so keep lines people added by hand
        # and swap only the one this sync owns.
        ours = mapping.budget_label(spec.code)
        lines = [li for li in existing_budget if li.get("label") != ours]
        if spec.budget_line:
            if currency and spec.budget_currency and currency.upper() != spec.budget_currency.upper():
                log.warning("%s: cost is in %s but the Initiative budget is in %s, skipping budget line",
                            spec.code, spec.budget_currency, currency)
            else:
                lines.append(spec.budget_line)

        steps = []
        if fields:
            steps.append(("details", lambda: self.lm.patch(initiative_id, **fields)))
        steps.append(("status", lambda: self.lm.set_status(initiative_id, spec.status)))
        steps.append(("priority", lambda: self.lm.set_priority(initiative_id, spec.priority)))
        if spec.fiscal_quarter:
            steps.append(("schedule", lambda: self.lm.set_quarter(initiative_id, spec.fiscal_quarter)))
        if lines != existing_budget:
            steps.append(("budget", lambda: self.lm.set_budget(initiative_id, lines)))

        failures = []
        for name, step in steps:
            try:
                step()
            except Exception as exc:
                log.error("%s: %s step failed: %s", spec.code, name, exc)
                failures.append(f"{name}: {exc}")
        if failures:
            raise StepsFailed(f"{len(failures)} of {len(steps)} steps failed, will retry next run: " + "; ".join(failures))

    def _retire(self, key: str, entry: dict, initiatives: dict, reason: str) -> None:
        """Handle an Action Item that was deleted or moved to a skipped status, per on_removed."""
        initiative_id = entry["initiative_id"]
        action = self.settings.on_removed
        if initiative_id not in initiatives or action == "ignore":
            entry["retired"] = True
            self.stats["left_alone"] += 1
            return
        if action == "delete":
            log.info("%s: %s, deleting Initiative %s", entry.get("code"), reason, initiative_id)
            self.lm.delete(initiative_id)
            # Forget it entirely, so the Action Item gets a fresh Initiative if it comes back.
            del self.items[key]
            self.stats["deleted"] += 1
            return
        log.info("%s: %s, setting Initiative %s to Declined", entry.get("code"), reason, initiative_id)
        self.lm.set_status(initiative_id, DECLINED)
        entry.update(status=DECLINED, retired=True, fingerprint=None)  # no fingerprint: re-push everything if it returns
        self.stats["declined"] += 1

    def _record(self, key: str, initiative_id: str, spec: mapping.InitiativeSpec, item: dict) -> None:
        previous = self.items.get(key) or {}
        self.items[key] = {
            "assigned_user_id": previous.get("assigned_user_id"),
            "initiative_id": initiative_id,
            "code": spec.code,
            "status": spec.status,
            "fingerprint": spec.fingerprint(),
            "cm_updated_at": item.get("updated_at"),
        }

    def _error(self, message: str) -> None:
        log.error(message)
        self.errors.append(message)
        self.stats["errors"] += 1


def _budget_lines(initiative: dict) -> list[dict]:
    """Existing one-time lines, trimmed to the fields the budget PUT accepts."""
    keep = ("label", "cost_subunits", "unit_count", "display_order", "cost_type")
    lines = (initiative.get("budget") or {}).get("line_items") or []
    return [{k: li[k] for k in keep if li.get(k) is not None} for li in lines]
