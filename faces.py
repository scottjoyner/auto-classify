#!/usr/bin/env python3
"""
faces.py — detect + embed faces in photos via insightface (LOCAL, onnxruntime,
no cloud — privacy-wall compliant). Clusters faces by cosine similarity.

Supports two modes:
  1. Container mode (default): reads from nextcloud container path
  2. Host mode (--photos-root): reads directly from host filesystem, uses NPU

When VitisAIExecutionProvider is available (Ryzen NPU), uses it automatically.
Falls back to CPUExecutionProvider if NPU is unavailable.

Pipeline:
  for each Media (images only):
    detect faces (buffalo_l) -> bboxes
    embed each face -> 512-d vector
  greedy cluster embeddings by cosine >= CLUSTER_THRESHOLD
  write:
    face_embeddings.jsonl  : {media_id, face_idx, bbox, cluster_id, embedding}
    face_clusters.jsonl     : {cluster_id, member_count}

Idempotent/resumable: tracks processed media_ids in faces.done; re-run skips them.

Usage:
  # Container mode (original):
  docker exec nextcloud /tmp/facevenv/bin/python /work/faces.py [--limit 200] [--year 2025]

  # Host mode (NPU-accelerated):
  python faces.py --photos-root /media/scott/SSD_4TB/nextcloud_stage [--limit 200] [--year 2025]
"""
import os, sys, json, argparse, traceback

# When running on host, ac_config is in the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_config as config

import numpy as np

CLUSTER_THRESHOLD = 0.5  # cosine; insightface face embeddings ~0.5+ = same person


def get_providers():
    """Auto-detect available ORT providers, preferring NPU over CPU.
    Falls back to CPU if VitisAI EP fails to initialize (known issue with
    complex multi-model graphs like insightface's buffalo_l on Linux)."""
    import onnxruntime as ort
    available = ort.get_available_providers()
    for pref in ["VitisAIExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]:
        if pref in available:
            return [pref]
    return ["CPUExecutionProvider"]


def init_face_analysis(providers):
    """Initialize insightface FaceAnalysis, falling back to CPU if NPU crashes.

    The VitisAI EP on Linux crashes (C++ abort) on some complex multi-model
    graphs like insightface's buffalo_l. We detect this by running a quick
    subprocess probe and fall back to CPU if it fails.
    """
    from insightface.app import FaceAnalysis

    if "VitisAIExecutionProvider" in providers:
        # Probe NPU in a subprocess (C++ abort can't be caught in-process)
        probe_ok = _probe_npu_for_insightface()
        if not probe_ok:
            print("[faces] NPU probe failed, using CPU", flush=True)
            providers = ["CPUExecutionProvider"]

    app = FaceAnalysis(name="buffalo_l", providers=providers)
    app.prepare(ctx_id=-1, det_size=(640, 640))
    return app, providers[0]


def _probe_npu_for_insightface():
    """Test if VitisAI EP can handle insightface models (subprocess probe)."""
    import subprocess, tempfile, textwrap
    script = textwrap.dedent("""\
        import os, sys
        os.environ.setdefault("RYZEN_AI_INSTALLATION_PATH", "/media/scott/data/ryzen-ai-venv")
        os.environ.setdefault("LD_PRELOAD", "/opt/xilinx/xrt/lib/libxrt_coreutil.so.2")
        ld = os.environ.get("LD_LIBRARY_PATH", "")
        for d in ["/media/scott/data/ryzen-ai-venv/lib/python3.12/site-packages/voe/lib",
                   "/media/scott/data/ryzen-ai-venv/onnxruntime/lib",
                   "/media/scott/data/ryzen-ai-venv/deployment/lib",
                   "/opt/xilinx/xrt/lib"]:
            if d not in ld:
                ld = f"{d}:{ld}" if ld else d
        os.environ["LD_LIBRARY_PATH"] = ld
        import onnxruntime as ort
        # Register VitisAI EP
        from onnxruntime.capi import _pybind_state
        lib = "/media/scott/data/ryzen-ai-venv/lib/python3.12/site-packages/voe/lib/libonnxruntime_vitisai_ep.so"
        ort.register_execution_provider_library("VitisAIExecutionProvider", lib)
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", providers=["VitisAIExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        print("NPU_PROBE_OK")
    """)
    try:
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=300,
            env={**os.environ,
                 "RYZEN_AI_INSTALLATION_PATH": "/media/scott/data/ryzen-ai-venv",
                 "LD_PRELOAD": "/opt/xilinx/xrt/lib/libxrt_coreutil.so.2",
                 "LD_LIBRARY_PATH": ":".join([
                     "/media/scott/data/ryzen-ai-venv/lib/python3.12/site-packages/voe/lib",
                     "/media/scott/data/ryzen-ai-venv/onnxruntime/lib",
                     "/media/scott/data/ryzen-ai-venv/deployment/lib",
                     "/opt/xilinx/xrt/lib",
                     os.environ.get("LD_LIBRARY_PATH", ""),
                 ])}
        )
        return "NPU_PROBE_OK" in r.stdout
    except Exception:
        return False


def setup_npu_env():
    """Set environment variables needed for VitisAI EP on Ryzen NPU."""
    if not hasattr(config, "RYZEN_AI_VENV"):
        return
    venv = config.RYZEN_AI_VENV
    os.environ["RYZEN_AI_INSTALLATION_PATH"] = venv
    os.environ.setdefault("LD_PRELOAD", "")
    xrt_coreutil = f"/opt/xilinx/xrt/lib/libxrt_coreutil.so.2"
    if xrt_coreutil not in os.environ.get("LD_PRELOAD", ""):
        os.environ["LD_PRELOAD"] = f"{xrt_coreutil} {os.environ['LD_PRELOAD']}".strip()
    lib_dirs = [
        f"{venv}/lib/python3.12/site-packages/voe/lib",
        f"{venv}/onnxruntime/lib",
        f"{venv}/deployment/lib",
        "/opt/xilinx/xrt/lib",
    ]
    current = os.environ.get("LD_LIBRARY_PATH", "")
    for d in lib_dirs:
        if d not in current:
            current = f"{d}:{current}" if current else d
    os.environ["LD_LIBRARY_PATH"] = current


def load_media(jsonl, year=None, limit=None):
    rows = []
    with open(jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["media_type"] != "image":
                continue
            if year and not (r["timestamp"] or "").startswith(year):
                continue
            rows.append(r)
    if limit:
        rows = rows[:limit]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--year")
    ap.add_argument("--photos-root", help="Host photos root (enables host mode with NPU)")
    ap.add_argument("--output-dir", help="Directory for JSONL output (default: _ingest under photos-root)")
    args = ap.parse_args()

    # Determine paths based on mode
    if args.photos_root:
        photos_root = args.photos_root
        ingest_dir = args.output_dir or os.path.join(photos_root, "_ingest")
    else:
        photos_root = config.PHOTOS_ROOT_IN_CONTAINER
        ingest_dir = config.INGEST_DIR_IN_CONTAINER

    jsonl_in = os.path.join(ingest_dir, "media_inventory.jsonl")
    emb_out = os.path.join(ingest_dir, "face_embeddings.jsonl")
    cluster_out = os.path.join(ingest_dir, "face_clusters.jsonl")
    done_file = os.path.join(ingest_dir, "faces.done")

    os.makedirs(ingest_dir, exist_ok=True)

    # Setup NPU if running on host
    if args.photos_root:
        setup_npu_env()

    # Detect providers and initialize face analysis
    import onnxruntime as ort
    providers = get_providers()
    print(f"[faces] ORT providers: {ort.get_available_providers()}", flush=True)
    print(f"[faces] using: {providers[0]}", flush=True)

    app, active_provider = init_face_analysis(providers)
    print(f"[faces] insightface ready (provider: {active_provider})", flush=True)

    media = load_media(jsonl_in, year=args.year)
    print(f"[faces] {len(media)} images total", flush=True)

    done = set()
    if os.path.exists(done_file):
        with open(done_file) as f:
            done = set(l.strip() for l in f if l.strip())

    # Filter out already-processed images, then apply limit
    media = [r for r in media if r["media_id"] not in done]
    if args.limit:
        media = media[:args.limit]
    print(f"[faces] {len(media)} images to process (skipped {len(done)} already done)", flush=True)

    # load existing embeddings to extend clusters
    clusters = {}  # cluster_id -> list of embeddings (np arrays)
    next_cid = 0
    if os.path.exists(cluster_out):
        with open(cluster_out) as f:
            for line in f:
                c = json.loads(line)
                clusters[c["cluster_id"]] = clusters.get(c["cluster_id"], [])
        next_cid = max(clusters.keys(), default=-1) + 1

    def assign_cluster(emb):
        nonlocal next_cid
        best, best_sim = None, -1
        for cid, embs in clusters.items():
            for e in embs:
                sim = float(np.dot(emb, e) / (np.linalg.norm(emb) * np.linalg.norm(e)))
                if sim > best_sim:
                    best, best_sim = cid, sim
        if best is not None and best_sim >= CLUSTER_THRESHOLD:
            return best
        cid = next_cid
        next_cid += 1
        clusters.setdefault(cid, []).append(emb)
        return cid

    n_faces = 0
    with open(emb_out, "a") as ef, open(done_file, "a") as df:
        for i, r in enumerate(media, 1):
            mid = r["media_id"]
            if mid in done:
                continue
            path = r["path"]
            try:
                if not os.path.exists(path):
                    df.write(mid + "\n"); continue
                from PIL import Image
                img = np.array(Image.open(path).convert("RGB"))
                faces_det = app.get(img)
                for fi, fc in enumerate(faces_det):
                    emb = fc.embedding.astype(float)
                    cid = assign_cluster(emb)
                    rec = {
                        "media_id": mid,
                        "face_idx": fi,
                        "bbox": [float(x) for x in fc.bbox],
                        "cluster_id": cid,
                        "embedding": emb.tolist(),
                    }
                    ef.write(json.dumps(rec) + "\n")
                    n_faces += 1
                df.write(mid + "\n")
            except Exception as e:
                print(f"[faces] ERR {mid}: {e}", file=sys.stderr, flush=True)
                df.write(mid + "\n")
            if i % 50 == 0:
                print(f"[faces] {i}/{len(media)} images | faces={n_faces} clusters={next_cid}", flush=True)

    # write cluster summary
    with open(cluster_out, "w") as cf:
        for cid, embs in clusters.items():
            cf.write(json.dumps({"cluster_id": cid, "member_count": len(embs)}) + "\n")
    print(f"[faces] DONE: {n_faces} faces across {next_cid} clusters", flush=True)


if __name__ == "__main__":
    main()
