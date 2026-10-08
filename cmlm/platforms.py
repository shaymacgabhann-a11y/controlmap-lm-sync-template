"""ControlMap reads and Lifecycle Manager reads/writes."""

from __future__ import annotations

import calendar
import itertools
import logging

from .api import ScalePadClient

log = logging.getLogger(__name__)


def quarter_bounds(year: int, quarter: int) -> tuple[str, str]:
    first_month, last_month = 3 * quarter - 2, 3 * quarter
    last_day = calendar.monthrange(year, last_month)[1]
    return f"{year}-{first_month:02d}-01T00:00:00Z", f"{year}-{last_month:02d}-{last_day:02d}T00:00:00Z"


class ControlMap:
    def __init__(self, client: ScalePadClient):
        self.client = client

    def clients(self) -> list[dict]:
        """Every ControlMap client, with its Action Item counts under action_summary."""
        return list(self.client.paginate_get("/controlmap/v1/clients/action-items-summary"))

    def action_items(self, client_id: str) -> list[dict]:
        return list(self.client.paginate_post(f"/controlmap/v1/clients/{client_id}/action-items/search"))


class LifecycleManager:
    """Initiative operations. With dry_run=True, writes are logged and skipped."""

    def __init__(self, client: ScalePadClient, dry_run: bool):
        self.client = client
        self.dry_run = dry_run
        self._fake_ids = (f"dry-run-{n}" for n in itertools.count(1))

    def clients(self) -> dict[str, str]:
        """Lifecycle Manager client id -> display name."""
        rows = self.client.paginate_get("/lifecycle-manager/v1/clients")
        return {r["client"]["client_id"]: r["client"]["display_name"] for r in rows}

    def users(self) -> list[dict]:
        return self.client.get("/lifecycle-manager/v1/users").get("data", [])

    def set_assignee(self, initiative_id: str, user_id: str) -> None:
        if not self._skip("assign", initiative_id, user_id):
            self.client.put(f"/lifecycle-manager/v1/initiatives/{initiative_id}/assigned-user", {"assigned_user_id": user_id})

    def initiatives(self, client_id: str) -> list[dict]:
        return list(
            self.client.paginate_get(
                "/lifecycle-manager/v2/initiatives",
                {"filter[client.id]": f"eq:{client_id}", "include_unscheduled": "true"},
            )
        )

    def create(self, client_id: str, name: str, summary_json: str) -> str:
        body = {"client_key": {"id": client_id}, "name": name, "executive_summary_json": summary_json}
        if self._skip("create", name):
            return next(self._fake_ids)
        return self.client.post("/lifecycle-manager/v1/initiatives", body)["id"]

    def patch(self, initiative_id: str, **fields) -> None:
        if not self._skip("patch", initiative_id, sorted(fields)):
            self.client.patch(f"/lifecycle-manager/v1/initiatives/{initiative_id}", fields)

    def set_status(self, initiative_id: str, status: str) -> None:
        if not self._skip("status", initiative_id, status):
            self.client.put(f"/lifecycle-manager/v1/initiatives/{initiative_id}/status", {"status": status})

    def set_priority(self, initiative_id: str, priority: str) -> None:
        if not self._skip("priority", initiative_id, priority):
            self.client.put(f"/lifecycle-manager/v1/initiatives/{initiative_id}/priority", {"priority": priority})

    def set_quarter(self, initiative_id: str, fiscal_quarter: dict) -> None:
        # The API rejects fiscal_quarter alongside dates and requires both dates, so a
        # quarter is sent as its first/last day with Quarter precision (how LM stores it).
        start, end = quarter_bounds(fiscal_quarter["year"], fiscal_quarter["quarter"])
        body = {"target_start_date": start, "target_end_date": end, "target_precision": "Quarter"}
        if not self._skip("schedule", initiative_id, f"{start[:10]}..{end[:10]}"):
            self.client.put(f"/lifecycle-manager/v1/initiatives/{initiative_id}/schedule", body)

    def set_budget(self, initiative_id: str, line_items: list[dict]) -> None:
        if not self._skip("budget", initiative_id, [li["label"] for li in line_items]):
            self.client.put(
                f"/lifecycle-manager/v1/initiatives/{initiative_id}/budget", {"budget_line_items": line_items}
            )

    def delete(self, initiative_id: str) -> None:
        if not self._skip("delete", initiative_id):
            self.client.request("DELETE", f"/lifecycle-manager/v1/initiatives/{initiative_id}")

    def _skip(self, action: str, *detail) -> bool:
        if self.dry_run:
            log.info("[dry-run] would %s %s", action, " ".join(map(str, detail)))
        return self.dry_run
