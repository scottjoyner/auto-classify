# batchrunner — priority-aware, idle-gated dispatcher for resumable batch loads
#
# WHY: the fleet has 18 cron jobs (several erroring) but no cross-job prioritizer that
# says "run the lowest-priority slice only when the host is idle." Long CPU jobs like the
# 41k-image face-ingestion (auto-classify/faces.py) should run as BATCH=5 work:
# sliced, resumable, and never preempt interactive/fleet compute.
#
# DESIGN (borrows from AssistX task enum + cron-kg-write pattern):
#   - Each batch load is a MANIFEST (batch_jobs/*.yaml): command, slice size, priority,
#     idle-gate, allowed-hours, max-concurrent, ETA estimate.
#   - batchrunner.py dispatch:
#       1. Load all manifests.
#       2. For each, compute "can run NOW?" from gates (host idle %, allowed hours,
#          no other batch running, fleet compute not needed).
#       3. Pick the HIGHEST-priority job that can run (priority 0=highest .. 5=batch).
#       4. Execute ONE SLICE of its command (resumable via done-file / --limit).
#       5. Record progress to state file; reschedule (cron drives re-entry).
#   - --dry-run: print the decision tree, execute NOTHING. Prep/validate without load.
#
# PRIORITY (aligned with AssistX enum): 0 CRITICAL, 1 HIGH, 2 MEDIUM, 3 LOW,
#   4 BACKGROUND, 5 BATCH. Lower number = runs sooner. Faces = 5 (BATCH).
#
# IDLE GATE: host CPU load avg(1) < idle_max_load AND current hour in allowed_hours.
# This keeps batch loads off prod hours / busy nodes.
#
# STATE: progress tracked in batch_state.json (slices done, last_run, status) so a
#   re-entry knows where to resume and when the job is complete.
#
# WIRING: a single cron (paused by default) calls `batchrunner.py dispatch` every
#   N minutes. It self-limits to one slice per tick, so even a "run now" just does
#   a safe chunk. To actually start the face load: set the manifest enabled=true and
#   unpause the cron.

import os, sys, json, time, subprocess, argparse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_DIR = os.path.join(HERE, "batch_jobs")
STATE_FILE = os.path.join(HERE, "batch_state.json")

# priority name -> rank (0 highest)
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "background": 4, "batch": 5}

def load_manifests():
    import glob, yaml
    out = []
    for f in sorted(glob.glob(os.path.join(BATCH_DIR, "*.yaml"))):
        with open(f) as fh:
            m = yaml.safe_load(fh)
        m["_file"] = os.path.basename(f)
        out.append(m)
    return out

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

def host_load_avg1():
    """Return 1-min load average (float)."""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0

def host_cpu_count():
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1

def can_run_now(m, st, other_running, dry=False):
    """Return (ok, reason). Gates: enabled, allowed_hours, idle load, no concurrent batch."""
    reasons = []
    if not m.get("enabled", False):
        return False, "disabled"
    mtype = m.get("type", "slice")
    # allowed hours
    ah = m.get("allowed_hours")  # e.g. "0-7,19-23" or None=any
    if ah:
        hr = datetime.datetime.now().hour
        if "-" in ah:
            ok_hour = any(
                int(a) <= hr <= int(b) for part in ah.split(",") for a, b in [part.split("-")]
            )
        else:
            ok_hour = hr in [int(x) for x in ah.split(",")]
        if not ok_hour:
            return False, f"outside allowed_hours ({ah})"
    # idle gate (slice jobs): host load
    if mtype == "slice":
        max_load = m.get("idle_max_load", 2.0)
        load = host_load_avg1()
        ncpu = host_cpu_count()
        load_pct = load / ncpu
        if load_pct > max_load:
            return False, f"host busy (load {load:.1f}/{ncpu}c = {load_pct:.0%} > {max_load:.0%})"
        if other_running:
            return False, f"another batch running ({other_running})"
    # service jobs: launch only if not already running
    elif mtype == "service":
        pidf = m.get("pidfile")
        if pidf and os.path.exists(pidf):
            try:
                with open(pidf) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                return False, f"already running (pid {pid})"
            except Exception:
                os.remove(pidf)
        # also idle-gate services so they don't spin up on a hot host
        max_load = m.get("idle_max_load", 1.5)
        load = host_load_avg1()
        ncpu = host_cpu_count()
        if load / ncpu > max_load:
            return False, f"host busy (load {load:.1f}/{ncpu}c)"
    # completion check
    prog = st.get(m["name"], {})
    if prog.get("status") == "complete":
        return False, "already complete"
    return True, "ok"

def run_slice(m, dry=False):
    """Execute one slice (type=slice) or launch/verify a service (type=service)."""
    mtype = m.get("type", "slice")
    cmd = m["command"]

    if mtype == "service":
        # launch in background, write pidfile
        pidf = m.get("pidfile")
        nohup = m.get("nohup", True)
        full = f"nohup {cmd} > {m.get('logfile', '/dev/null')} 2>&1 & echo $!" if nohup else cmd
        print(f"[batchrunner] SERVICE {m['name']}: {full}", flush=True)
        if dry:
            print("[batchrunner] dry-run: not launching", flush=True)
            return "dry"
        if pidf:
            # launch and capture pid
            r = subprocess.run(f"{cmd} & echo $! > {pidf}", shell=True,
                                capture_output=True, text=True)
            print(f"[batchrunner] launched, pidfile={pidf}: {r.stdout.strip()[-200:]}", flush=True)
        else:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return "ok"

    # slice type
    slice_arg = m.get("slice_arg", "--limit")
    slice_size = m.get("slice_size", 200)
    full = cmd
    if slice_arg and slice_size:
        full = f"{cmd} {slice_arg} {slice_size}"
    print(f"[batchrunner] SLICE {m['name']}: {full}", flush=True)
    if dry:
        print("[batchrunner] dry-run: not executing", flush=True)
        return "dry"
    timeout = m.get("slice_timeout_sec", 1800)
    try:
        r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr)[-2000:]
        print(f"[batchrunner] exit={r.returncode}\n{out}", flush=True)
        return "ok" if r.returncode == 0 else "error"
    except subprocess.TimeoutExpired:
        print(f"[batchrunner] slice timed out after {timeout}s", file=sys.stderr, flush=True)
        return "timeout"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="dispatch")
    ap.add_argument("--dry-run", action="store_true", help="print decisions, execute nothing")
    ap.add_argument("--force", help="run a specific job name regardless of gates (dry unless --execute)")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(BATCH_DIR):
        print(f"[batchrunner] no {BATCH_DIR}", file=sys.stderr); sys.exit(1)

    manifests = load_manifests()
    if not manifests:
        print("[batchrunner] no manifests"); sys.exit(0)
    st = load_state()

    # detect "another SLICE batch running" via pidfile (services don't block slices;
    # they're independent background workers).
    pidfile = os.path.join(HERE, "batchrunner.pid")
    other = None
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            other = f"pid {pid}"
        except Exception:
            os.remove(pidfile)

    # evaluate each manifest
    evaluated = []
    for m in manifests:
        ok, reason = can_run_now(m, st, other if m.get("type", "slice") == "slice" else None)
        evaluated.append((m, ok, reason))

    print("=== batchrunner dispatch ===", flush=True)
    for m, ok, reason in sorted(evaluated, key=lambda x: PRIORITY_RANK.get(x[0].get("priority","batch"), 5)):
        print(f"  [{m.get('priority','batch'):>9}] {m['name']:<28} {'RUN' if ok else 'skip':<5} ({reason})", flush=True)

    if args.action == "status":
        print("--- progress ---", flush=True)
        for m in manifests:
            prog = st.get(m["name"], {})
            print(f"  {m['name']:<28} {prog.get('status','pending')} slices={prog.get('slices',0)} last={prog.get('last_run','-')}", flush=True)
        return

    if args.force:
        m = next((x for x in manifests if x["name"] == args.force), None)
        if not m:
            print(f"[batchrunner] no such job: {args.force}", file=sys.stderr); sys.exit(1)
        print(f"[batchrunner] FORCE {args.force} execute={args.execute}", flush=True)
        if not args.execute and not args.dry_run:
            print("[batchrunner] --force without --execute is a dry-run", flush=True)
        result = run_slice(m, dry=not args.execute)
        # preview post_slice so --force --dry-run is honest about what follows a slice
        if m.get("post_slice"):
            print(f"[batchrunner] POST_SLICE {m['name']}: {m['post_slice'][:160]}", flush=True)
            if args.dry_run:
                print("[batchrunner] dry-run: post_slice not executed", flush=True)
        return

    # dispatch ALL runnable jobs (one action each per tick). Services (background
    # workers) and slices (one-shot) are independent — run each that's gated-open.
    runnable = [(m, ok, r) for m, ok, r in evaluated if ok]
    if not runnable:
        print("[batchrunner] nothing runnable now (all gated).", flush=True)
        return

    # order: services first (cheap to launch), then slices by priority rank asc
    def sort_key(x):
        m = x[0]
        is_svc = m.get("type", "slice") == "service"
        return (0 if is_svc else 1, PRIORITY_RANK.get(m.get("priority", "batch"), 5))
    runnable.sort(key=sort_key)

    for m, ok, reason in runnable:
        print(f"[batchrunner] -> {m['name']} (type={m.get('type','slice')}, "
              f"priority {m.get('priority')})", flush=True)
        # claim pidfile only for slice jobs (services are background, don't hold the lock)
        if m.get("type", "slice") == "slice":
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))
        try:
            result = run_slice(m, dry=args.dry_run)
        finally:
            if m.get("type", "slice") == "slice" and os.path.exists(pidfile):
                os.remove(pidfile)

        # post_slice hook: e.g. copy embeddings out + graph-write so progress is
        # queryable incrementally. Runs after a real (non-dry) slice.
        if (result not in ("dry",) and m.get("post_slice") and m.get("type","slice")=="slice"):
            ps = m["post_slice"]
            print(f"[batchrunner] POST_SLICE {m['name']}: {ps[:160]}", flush=True)
            if args.dry_run:
                print("[batchrunner] dry-run: post_slice not executed", flush=True)
            else:
                try:
                    r = subprocess.run(ps, shell=True, capture_output=True, text=True,
                                        timeout=m.get("post_slice_timeout_sec", 600))
                    print(f"[batchrunner] post_slice exit={r.returncode} "
                          f"{(r.stdout+r.stderr)[-800:]}", flush=True)
                except subprocess.TimeoutExpired:
                    print("[batchrunner] post_slice timed out", file=sys.stderr, flush=True)

        # update state
        prog = st.get(m["name"], {})
        if m.get("type", "slice") == "slice":
            prog["slices"] = prog.get("slices", 0) + (0 if result == "dry" else 1)
        prog["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        prog["last_result"] = result

        # completion heuristic for slice jobs:
        #   if manifest declares done_file + complete_when=done_file_stable,
        #   compare done-file line count across ticks. Stable (no growth) after
        #   >=1 run => the resumable job has drained => mark complete.
        done_file = m.get("done_file")
        if m.get("type", "slice") == "slice" and done_file and \
                m.get("complete_when") == "done_file_stable":
            try:
                cur = sum(1 for _ in open(done_file)) if os.path.exists(done_file) else 0
            except Exception:
                cur = prog.get("done_lines", 0)
            prev = prog.get("done_lines")
            prog["done_lines"] = cur
            if prev is not None and prev == cur and prog.get("slices", 0) >= 1:
                prog["status"] = "complete"
                print(f"[batchrunner] {m['name']} COMPLETE "
                      f"(done_file stable at {cur} lines)", flush=True)
            else:
                prog["status"] = "running"
        else:
            prog["status"] = "complete" if result == "complete" else "running"
        st[m["name"]] = prog
    save_state(st)
    print(f"[batchrunner] dispatched {len(runnable)} job(s). state saved.", flush=True)

if __name__ == "__main__":
    main()
