# PROCEDURE — batch-load scheduler (`batchrunner`)

How to operate the priority-aware, idle-gated batch-load dispatcher in this repo.
Code: `batchrunner.py`. Manifests: `batch_jobs/*.yaml`. Cron: `batchrunner-dispatch`
(every 15 min, id `a26b8ac25999`). State: `batch_state.json`.

## What it is

A generic dispatcher that runs long/resumable batch jobs (photo face-ingest, auto-ingest
pipeline, re-indexes, backfills) WITHOUT hammering the host or preempting fleet compute.
Each job is a manifest; the dispatcher picks runnable jobs each tick, runs ONE action per
job, and records state. It never runs a heavy job unless the host is idle.

Two job types:
- **`slice`** — one-shot resumable command driven in chunks (`faces.py --limit 200`).
  Gated by idle load + allowed hours. Auto-completes when its `done_file` stops growing.
- **`service`** — a long-lived background worker that is itself idle-gated + resumable
  (e.g. auto-ingest `run_worker.sh`). batchrunner only LAUNCHES it when host idle + not
  already running; otherwise skips.

Priority aligns with the AssistX task enum: `critical=0 high=1 medium=2 low=3
background=4 batch=5`. Lower runs sooner. Idle gate: 1-min host load < `idle_max_load`
per CPU.

## Commands

```
# preview the decision tree, execute nothing (safe default)
python batchrunner.py dispatch --dry-run

# show per-job state (slices run, done-file lines, status, failures)
python batchrunner.py status

# preview a specific job's slice + post_slice without running
python batchrunner.py --force faces_full --dry-run

# actually force-run one slice of a job (ignores gates; still respects dry unless --execute)
python batchrunner.py --force faces_full --execute
```

`--dry-run` never executes, never writes `batch_state.json`. Always run it first.

## Adding a new batch job

1. Copy `batch_jobs/faces_full.yaml` to `batch_jobs/<name>.yaml`.
2. Set fields:
   - `name` (unique), `enabled: false` (start paused), `priority` (use the enum).
   - `type: slice` or `service`.
   - `command` (the thing to run). For `slice`, the dispatcher appends
     `--limit <slice_size>` (override with `slice_arg`). The command MUST be resumable
     (its own done-file / state) so re-runs are safe.
   - `slice_size`, `slice_timeout_sec` (slice only).
   - `idle_max_load`, `allowed_hours` (slice; service has a lighter idle gate).
   - `pidfile`, `logfile` (service).
   - `post_slice` (optional, slice): shell cmd after each slice (e.g. copy artifacts out
     + graph-write). Not run in dry-run.
   - `done_file` + `complete_when: done_file_stable` (slice): auto-marks complete when the
     done-file line count stops growing between ticks.
   - `max_failures`, `backoff_min`, `stuck_ticks` (resilience; see below).
3. Validate: `python batchrunner.py dispatch --dry-run` (must parse, show your job).
4. Commit. Do NOT enable yet.

## Starting / stopping a job

- **Start:** set `enabled: true` in the manifest, commit+push. The cron self-starts it on
  the next idle tick. No other action needed.
- **Pause:** set `enabled: false`, commit. In-flight slice finishes; no new slices start.
- **Stop a service:** `touch /home/scott/git/auto-ingest/worker.stop` (for auto-ingest),
  or kill its pid from the pidfile.

## Resilience behavior

- **Backoff:** after a slice fails (`error`/`timeout`), consecutive failures increment.
  The dispatcher waits `min(fails,5) * backoff_min` minutes before retrying, so a broken
  job doesn't hammer the host every 15 min. A success resets the counter.
- **Max failures:** after `max_failures` consecutive failures, the job is skipped with
  "max_failures reached" — needs inspection. Clear it by editing `batch_state.json`
  (`failures: 0`) or re-running with `--force --execute`.
- **Stuck detection:** if a `slice` job is `running` but its `done_file` hasn't grown for
  `stuck_ticks` consecutive ticks AND no error is counted, status flips to `STUCK` and the
  job stops dispatching. Progress is preserved. Resume by clearing `status` (set to
  `running`) in `batch_state.json` after diagnosing why the done-file isn't advancing
  (e.g. the underlying command is silently no-op-ing, or the done-file path is wrong).
- **Completion:** a `slice` job with `done_file_stable` auto-flips to `complete` when the
  done-file is stable for one tick after ≥1 slice — no manual "is it done?" check.

## Diagnosing a stuck / failed job

```
python batchrunner.py status          # see status, failures, done_lines, last_error
cat batch_state.json                  # raw state
# for faces: check the container-side done-file grew
docker exec nextcloud wc -l /var/www/html/data/admin/files/Photos/_ingest/faces.done
# resume after fixing root cause:
#   edit batch_state.json: set that job's status -> "running", failures -> 0
#   (or delete its block entirely to reset)
```

## Cron

`batchrunner-dispatch` (every 15 min) runs `batchrunner_dispatch.sh`, which calls
`batchrunner.py dispatch`. The script lives at `~/.hermes/scripts/batchrunner_dispatch.sh`.
The cron is ENABLED but ALL manifests are `enabled: false`, so it reports "nothing
runnable now" until a manifest is flipped on. To disable the whole scheduler, pause the
cron job.

## Env truths (learned the hard way)

- Nextcloud photo volume is root-owned inside the container; only `docker exec nextcloud`
  (root) can read it. Host sees it empty. Batch commands that touch Nextcloud MUST run
  via `docker exec nextcloud`.
- insightface needs `opencv-python-headless` (not `opencv-python` — needs libGL which is
  absent). Build its venv INSIDE the container (host-built venvs fail: glibc mismatch on
  Debian 13).
- EXIF `DateTimeOriginal` is in the sub-IFD; GPS coords are `IFDRational` (cast float()).
- The MCP Neo4j read query has a 120s circuit-breaker that returns STALE/partial counts
  (e.g. 160 instead of 41,040). Verify graph counts with the Python Bolt driver, not MCP.
- `faces.py` is resumable via `_ingest/faces.done` (one media_id per line).
