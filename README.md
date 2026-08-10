# parallel-scraper-worker

Public **Phase 2 deep-scrape worker** for the parallel Google Maps scraper. It runs the
Playwright deep-scrape on free public-repo GitHub Actions minutes and talks to a small
private **lease/submit API** for all state.

## Security model

This repo holds **no database or Google Drive credentials** — only:

| Name | Kind | Purpose |
|------|------|---------|
| `WORKER_TOKEN` | repo **secret** | bearer token for the lease/submit API |
| `WORKER_API_BASE` | repo **variable** | HTTPS base URL of the API |
| `PHASE2_SHOT_BLOB_SAS` | repo **secret** (optional) | **write-only** (`sp=cw`) Azure Blob container SAS URL for screenshot staging — no account key, no read/list/delete |

The worker leases `placeId`s from `POST /phase2/lease`, scrapes each with Playwright, and
submits results to `POST /phase2/results`. The API (running privately on a VM, the **only**
component with DB creds) writes to Azure SQL. Each lease carries an `attempts` version that
the API checks on submit, so a stale or leaked worker can't overwrite newer state.

Screenshots are captured but never uploaded to Drive from public compute. Staging, in
priority order (`cloud/blob_shots.py` / `_sink_shots`):
1. **Azure Blob** (when `PHASE2_SHOT_BLOB_SAS` is set): each PNG is PUT to
   `<container>/<run_id>/<place_id>_<kind>.png` during the scrape. Blob is the
   **permanent store**; a private indexer records each blob's URL in the DB
   continuously — even while shards are still running.
2. **Build artifacts** (fallback, and catch-all for failed blob PUTs): PNGs + manifest
   are uploaded as `screenshots-<run_id>-shard-<n>` and drained later.
Either way, Drive/DB credentials never exist on public compute.

A CI `guard` job (`scripts/check_no_secrets.sh`) fails the build if any credential file,
private-only module (`sql_state.py`/`api.py`/`storage.py`), or client-data path appears.

## Running

Dispatch the workflow (Actions tab → **phase2-scrape** → Run workflow), or:

```bash
gh workflow run phase2-scrape.yml -R <owner>/parallel-scraper-worker \
  -f run_id=<run_id> -f shards=12 -f max_minutes=300
```

The `plan` job asks the API how many placeIds remain and fans out `shards` runners; each is
a distinct datacenter IP at low per-IP rate. It's resumable — re-dispatch to drain more.

## Layout

```
src/parallel_scraper/        generic Maps scraper engine (no secrets, no client data)
cloud/phase2_worker_api.py   API-mode worker (HTTP state backend)
cloud/http_state.py          HTTP client for the lease/submit API
cloud/phase2_common.py       creds-free worker helpers
.github/workflows/phase2-scrape.yml
scripts/check_no_secrets.sh  CI secret/forbidden-path guard
```

The engine + `cloud/` subset are synced one-way from the private repo
(`tools/sync_public_worker.sh`); do not edit them here directly.
