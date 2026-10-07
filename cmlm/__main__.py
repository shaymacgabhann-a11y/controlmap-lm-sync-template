"""CLI: python -m cmlm [--live] [--client NAME] [--list-clients]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import clients as client_matching
from . import settings as settings_mod
from .api import ApiError, ScalePadClient
from .engine import Syncer
from .platforms import ControlMap, LifecycleManager

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("cmlm")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync ControlMap Action Items into Lifecycle Manager Initiatives.")
    parser.add_argument("--live", action="store_true", help="actually write to Lifecycle Manager (default: dry run)")
    parser.add_argument("--list-clients", action="store_true", help="show ControlMap clients and their Lifecycle Manager matches, then exit")
    parser.add_argument("--client", help="only sync this ControlMap client name")
    parser.add_argument("--config", default=ROOT / "config.yaml", type=Path)
    parser.add_argument("--state", default=ROOT / "state" / "state.json", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    _load_dotenv(ROOT / ".env")

    try:
        settings = settings_mod.load(args.config)
    except settings_mod.ConfigError as exc:
        log.error("config.yaml problem: %s", exc)
        return 2

    api_key = os.environ.get("SCALEPAD_API_KEY")
    if not api_key:
        log.error("SCALEPAD_API_KEY is not set (put it in .env locally, or add it as a GitHub Actions secret)")
        return 2

    client = ScalePadClient(api_key, region=settings.region)
    cm, lm = ControlMap(client), LifecycleManager(client, dry_run=not args.live)

    try:
        cm_clients, lm_clients = cm.clients(), lm.clients()
    except ApiError as exc:
        log.error("Could not list clients: %s", exc)
        if exc.status in (401, 403):
            log.error("Check that the API key is valid and has ControlMap and Lifecycle Manager access.")
        return 2

    if args.list_clients:
        _print_client_report(client_matching.report(settings, cm_clients, lm_clients))
        return 0

    pairs, problems = client_matching.resolve(settings, cm_clients, lm_clients)
    if args.client:
        pairs = [p for p in pairs if p.controlmap_name.lower() == args.client.lower()]
    for problem in problems:
        log.warning("Skipping client: %s", problem)
    if not pairs:
        log.error("No clients to sync. Run with --list-clients to see what's available, then edit config.yaml.")
        return 2

    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    syncer = Syncer(cm, lm, state, settings)

    log.info("Mode: %s, %d client(s), on_removed=%s", "LIVE" if args.live else "DRY RUN (no writes)", len(pairs), settings.on_removed)
    try:
        for pair in pairs:
            try:
                syncer.sync_client(pair)
            except Exception as exc:  # one client failing shouldn't stop the others
                syncer._error(f"{pair.controlmap_name}: {exc}")
    finally:
        # Save even after a crash so Initiatives already created aren't created again.
        if args.live:
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    _report(syncer, live=args.live, clients=len(pairs), problems=problems)
    return 1 if syncer.errors else 0


def _print_client_report(rows: list[client_matching.ClientRow]) -> None:
    header = ["Will sync", "ControlMap client", "Action Items", "Lifecycle Manager client", "Note"]
    table = [
        [
            "yes" if r.selected and r.lm_client_id else "no",
            r.controlmap_name,
            "" if r.action_items is None else str(r.action_items),
            r.lm_client_label or "-",
            r.note,
        ]
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h) for i, h in enumerate(header)]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    for row in table:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))
    print(f"\n{sum(r[0] == 'yes' for r in table)} of {len(table)} ControlMap clients will sync with the current config.yaml")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## ControlMap clients", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in table]
        with open(summary_path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


def _report(syncer: Syncer, live: bool, clients: int, problems: list[str]) -> None:
    order = ["created", "updated", "declined", "deleted", "left_alone", "unchanged", "skipped", "missing_in_lm", "errors"]
    rows = [(k, syncer.stats.get(k, 0)) for k in order]
    log.info("Summary (%d clients): %s", clients, ", ".join(f"{k}={v}" for k, v in rows))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [f"## ControlMap → Lifecycle Manager ({'live' if live else 'dry run'}, {clients} clients)", "",
                 "| Result | Count |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in rows]
        if problems:
            lines += ["", "### Clients skipped", *[f"- {p}" for p in problems]]
        if syncer.errors:
            lines += ["", "### Errors", *[f"- {e}" for e in syncer.errors]]
        with open(summary_path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


if __name__ == "__main__":
    sys.exit(main())
