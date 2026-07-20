#!/usr/bin/env python3
"""
faces.py — detect + embed faces in Nextcloud photos via insightface (LOCAL, onnxruntime,
no cloud — privacy-wall compliant). Clusters faces by cosine similarity.

RUN INSIDE the nextcloud container (reads the photo volume; insightface + opencv-headless
live in /tmp/facevenv there). Outputs JSONL into the volume _ingest/ dir.

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
  docker exec nextcloud /tmp/facevenv/bin/python /work/faces.py [--limit 200] [--year 2025]
"""
import os, sys, json, argparse, traceback
sys.path.insert(0, "/")
import ac_config as config
from insightface.app import FaceAnalysis
import numpy as np

PHOTOS_ROOT = "/var/www/html/data/admin/files/Photos"
INGEST_DIR = "/var/www/html/data/admin/files/Photos/_ingest"
JSONL_IN = os.path.join(INGEST_DIR, "media_inventory.jsonl")
EMB_OUT = os.path.join(INGEST_DIR, "face_embeddings.jsonl")
CLUSTER_OUT = os.path.join(INGEST_DIR, "face_clusters.jsonl")
DONE_FILE = os.path.join(INGEST_DIR, "faces.done")

CLUSTER_THRESHOLD = 0.5  # cosine; insightface face embeddings ~0.5+ = same person

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
    args = ap.parse_args()

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    print("[faces] insightface ready", flush=True)

    media = load_media(JSONL_IN, year=args.year, limit=args.limit)
    print(f"[faces] {len(media)} images to process", flush=True)

    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(l.strip() for l in f if l.strip())

    # load existing embeddings to extend clusters
    clusters = {}  # cluster_id -> list of embeddings (np arrays)
    next_cid = 0
    if os.path.exists(CLUSTER_OUT):
        with open(CLUSTER_OUT) as f:
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
    with open(EMB_OUT, "a") as ef, open(DONE_FILE, "a") as df:
        for i, r in enumerate(media, 1):
            mid = r["media_id"]
            if mid in done:
                continue
            path = r["path"]
            try:
                if not os.path.exists(path):
                    df.write(mid + "\n"); continue
                from PIL import Image
                import numpy as np
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
    with open(CLUSTER_OUT, "w") as cf:
        for cid, embs in clusters.items():
            cf.write(json.dumps({"cluster_id": cid, "member_count": len(embs)}) + "\n")
    print(f"[faces] DONE: {n_faces} faces across {next_cid} clusters", flush=True)

if __name__ == "__main__":
    main()
