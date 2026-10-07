# ControlMap → Lifecycle Manager sync

Turns your ControlMap **Action Items** into Lifecycle Manager **Initiatives** on each client's roadmap, and keeps them up to date every night. It runs for free on GitHub Actions; there's no server to host.

- New Action Item → new Initiative (`AI-12 · <weakness name>`) with status, priority, quarter, hours and cost.
- Action Item changes → the Initiative is updated.
- Action Item deleted or marked Not Applicable → the Initiative is set to Declined (configurable).
- Edits your team makes in Lifecycle Manager stay put until the Action Item itself changes.

## What you need

- ScalePad **ControlMap** and **Lifecycle Manager**.
- A ScalePad **API key** with access to both.
- A free **GitHub** account.

## Setup (about 15 minutes, no coding)

### 1. Make your own copy

At the top of this page, click **Use this template → Create a new repository**. Choose your account, give it a name, and set it to **Private**: run logs show client names and Action Item titles.

### 2. Add your API key

In your new repository: **Settings → Secrets and variables → Actions → Secrets tab → New repository secret**.

- Name: `SCALEPAD_API_KEY`
- Secret: your ScalePad API key

GitHub never shows secrets in logs, and they aren't copied to forks.

### 3. See your clients

Go to **Actions → Nightly sync → Run workflow**, pick **list-clients**, and click **Run workflow**. When it finishes, open the run. The summary lists every ControlMap client, how many Action Items it has, and the Lifecycle Manager client it matches.

> If GitHub asks you to enable Actions on the Actions tab first, click **I understand my workflows, go ahead and enable them**.

### 4. Choose clients

Open `config.yaml` and click the pencil icon to edit it in GitHub. Replace the placeholder under `clients:` with one or two ControlMap client names to start with:

```yaml
clients:
  - Contoso Ltd
```

Click **Commit changes**. Later you can widen this to `clients: all`.

### 5. Do a dry run

**Actions → Nightly sync → Run workflow → dry-run**. Nothing is written. The log shows each Initiative it *would* create or update, and the summary shows the totals. Check that the names, statuses and clients look right.

### 6. Go live

1. Run it once by hand with **live**. Open the client's roadmap in Lifecycle Manager and check the new Initiatives.
2. To turn on the nightly schedule, go to **Settings → Secrets and variables → Actions → Variables tab → New repository variable**: name `SYNC_LIVE`, value `true`.

That's it. The sync runs every night at 07:00 UTC. Until `SYNC_LIVE` is set, nightly runs are dry runs.

## Configuration

Everything is in [`config.yaml`](config.yaml), and each option is explained in comments there.

| Setting | What it does | Default |
|---|---|---|
| `region` | ScalePad API region: `us`, `eu`, `ca`, `au` | `us` |
| `clients` | `all`, or a list of ControlMap client names | placeholder |
| `exclude` | Clients to skip when using `clients: all` | none |
| `on_removed` | When an Action Item is deleted or skipped: `decline`, `delete` (permanent) or `ignore` | `decline` |
| `mapping.status` | ControlMap status → Lifecycle Manager status | see file |
| `mapping.skip_statuses` | ControlMap statuses that never get an Initiative | Not Applicable |
| `mapping.priority` | ControlMap priority → Lifecycle Manager priority | Critical/High → High |

If `config.yaml` has a mistake, the run stops immediately with a message saying what's wrong.

### When client names don't match

ControlMap and Lifecycle Manager share ScalePad client IDs, so clients match automatically even when their names differ. If list-clients shows *"no Lifecycle Manager client with the same ID"*, pin the client to the right one. The ID is in the client's Lifecycle Manager URL: `lm.scalepad.com/clients/<this-part>/…`

```yaml
clients:
  - name: Contoso Ltd
    lifecycle_manager_id: 00000000-0000-0000-0000-000000000000
```

### Changing the schedule

Edit the `cron` line in [`.github/workflows/sync.yml`](.github/workflows/sync.yml). For example, `0 9 * * 1-5` runs at 09:00 UTC on weekdays ([crontab.guru](https://crontab.guru) helps).

## Field mapping

| ControlMap Action Item | Lifecycle Manager Initiative |
|---|---|
| Code + weakness name | Name, e.g. `AI-12 · MFA not enforced` |
| Weakness description, corrective action, implementation notes | Executive summary |
| Status | Status (via `mapping.status`) |
| Priority | Priority (via `mapping.priority`) |
| Planned end date, else due date, else planned start date, else roadmap (3/6/12 months) | Calendar quarter on the roadmap |
| Effort in hours | Estimated hours |
| Cost | One-time investment line `ControlMap AI-12 remediation` (other budget lines are kept) |

Action Items with no dates or roadmap appear as unscheduled Initiatives.

## How it avoids duplicates

`state/state.json` records which Initiative belongs to which Action Item. Live runs commit it back to your repository automatically. If it's ever lost, the sync recognises existing Initiatives by their `AI-12 ·` name prefix instead of creating new ones, so **don't remove the code from Initiative names**.

If someone deletes a synced Initiative in Lifecycle Manager, the sync logs it and doesn't recreate it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Add your ScalePad API key…" | Step 2: the secret must be named exactly `SCALEPAD_API_KEY`. |
| HTTP 401 / 403 | The key is invalid or lacks ControlMap or Lifecycle Manager access. |
| "not found in ControlMap" | Check the client name against list-clients. |
| "Commit state" step fails | Your organisation may block Actions from pushing to `main`. Allow it under **Settings → Actions → General → Workflow permissions → Read and write**, or remove branch protection from `main`. |
| Re-running an old run uses old settings | **Re-run jobs** repeats the original commit. Use **Run workflow** instead. |
| Nightly runs stopped | GitHub pauses schedules in repositories with no activity for 60 days. Re-enable on the Actions tab. |

## Running locally (optional)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
cp .env.example .env               # then paste your key into .env
.venv/bin/python -m cmlm --list-clients
.venv/bin/python -m cmlm           # dry run
.venv/bin/python -m cmlm --live
.venv/bin/pytest
```

If you run live locally, commit `state/state.json` afterwards.

## Project layout

| Path | Purpose |
|---|---|
| `config.yaml` | Your settings |
| `cmlm/settings.py` | Loads and validates `config.yaml` |
| `cmlm/clients.py` | Matches ControlMap clients to Lifecycle Manager clients |
| `cmlm/mapping.py` | Action Item → Initiative field translation |
| `cmlm/engine.py` | Create / update / retire logic and state tracking |
| `cmlm/platforms.py` | ScalePad API calls; dry runs turn writes into log lines |
| `cmlm/api.py` | HTTP client: auth, pagination, retries |
