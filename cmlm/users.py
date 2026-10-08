"""Match a ControlMap responsible person to a Lifecycle Manager user."""

from __future__ import annotations

from collections import defaultdict


class UserDirectory:
    def __init__(self, lm_users: list[dict], overrides: dict[str, str] | None = None):
        self.by_email = {u["email"].strip().lower(): u for u in lm_users if u.get("email")}
        self.by_name: dict[str, list[dict]] = defaultdict(list)
        for u in lm_users:
            name = _full_name(u.get("first_name"), u.get("last_name"))
            if name:
                self.by_name[name].append(u)
        # ControlMap email or name (lowercase) -> Lifecycle Manager email (lowercase)
        self.overrides = {k.strip().lower(): v.strip().lower() for k, v in (overrides or {}).items()}

    def resolve(self, person) -> tuple[str | None, str]:
        """Return (LM user id or None, how it matched / why it didn't)."""
        if isinstance(person, str):
            person = {"name": person}
        if not person or not (person.get("email") or person.get("name")):
            return None, "no responsible person in ControlMap"
        email = (person.get("email") or "").strip().lower()
        name = " ".join((person.get("name") or "").lower().split())

        target = self.overrides.get(email) or self.overrides.get(name)
        if target:
            user = self.by_email.get(target)
            if user:
                return user["user_id"], "override"
            return None, f"override points to {target}, which isn't a Lifecycle Manager user"

        if email and email in self.by_email:
            return self.by_email[email]["user_id"], "email"

        matches = self.by_name.get(name, [])
        if len(matches) == 1:
            return matches[0]["user_id"], "name"
        label = person.get("email") or person.get("name")
        if len(matches) > 1:
            return None, f"{len(matches)} Lifecycle Manager users are named {person.get('name')!r}; pin {label} under assignees.overrides"
        return None, f"no Lifecycle Manager user matches {label}"


def _full_name(first, last) -> str:
    return " ".join(f"{first or ''} {last or ''}".lower().split())
