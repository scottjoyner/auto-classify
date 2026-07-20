# auto-classify

Ingest Scott's Nextcloud photo/video archive into Neo4j so media becomes
graph-searchable by **time**, **location**, and **person**.

## What it does

Nextcloud `Photos/` (117 GB, ~41k files, `YYYY/MM` layout, 2007→2025) is the iOS
instant-upload feed. This pipeline turns those files into first-class `(Media)` nodes
in Neo4j, linked to:

- **time** — EXIF `DateTimeOriginal` (92% coverage) or filename parse for videos
- **location** — EXIF GPS (86% coverage) joined by coordinate proximity to the
  existing `SummaryPlace` nodes (reuses the `neo4j-summary-geo-clustering` topology);
  photos without GPS fall back to contemporaneous PhoneLog GPS (±2h)
- **person** — `insightface` (local, onnxruntime, no cloud) detects + embeds faces,
  clusters them; Scott labels a cluster once → `(Person)` node

Then: *"photos of Lexi at the beach in June 2025"* is a one-hop Cypher query.

## Stages (each is a git checkpoint)

| Script | Runs | Purpose |
|--------|------|---------|
| `extract_metadata.py` | inside nextcloud container | walk Photos/, EXIF → `media_inventory.jsonl` |
| `graph_write.py` | host (x1-370) | `MERGE (Media)` + GPS→`SummaryPlace` join + time fallback |
| `faces.py` | inside nextcloud container | insightface detect/embed/cluster → `face_clusters.jsonl` |
| `graph_faces.py` | host (x1-370) | `MERGE (Person)`/`(FaceCluster)` + `DEPICTS` edges |

## Why run extraction inside the container

The Nextcloud data volume (`docker-compose_nextcloud-data`) is root-owned; only the
nextcloud container (running as root) can read `/var/www/html/data/admin/files/Photos`.
So `extract_metadata.py` / `faces.py` execute via
`docker exec nextcloud /tmp/facevenv/bin/python /work/<script>.py` and write JSONL into
the volume's `_ingest/` dir, retrieved with `docker cp`. Graph writes run on the host
where Neo4j Bolt (`100.64.43.123:7687`) is reachable.

## Environment notes (verified 2026-07-20)

- Container python is 3.13 (Debian 13); build the venv **inside** the container
  (`python3 -m venv /tmp/facevenv` + `pip install Pillow insightface onnxruntime
  opencv-python-headless`). Host-built venvs don't load (glibc/binary mismatch).
- `insightface` needs `opencv-python-headless` (not `opencv-python` — no libGL in slim).
- EXIF: `DateTimeOriginal` is in the Exif **sub-IFD** (0x8769, tag 36867); GPS coords
  are `IFDRational` → cast `float()`. Sign by N/S/E/W ref.
- File types: 29453 jpg, 9470 png, 1386 mov, 980 mp4, 57 jpeg. No HEIC.

## Status

- [x] **Checkpoint 1** (`77835fc`): `extract_metadata.py` — full scan of 41,246 files →
      41,040 unique Media records. 100% dated, 55% GPS, 0 errors, spans 1929→2026.
- [x] **Checkpoint 2** (`e59b88d`): `graph_write.py` — 41,040 `(Media)` nodes +
      12,660 `LOCATED_AT` (EXIF GPS→SummaryPlace) + 1,955 `CAPTURED_AT_TIME`
      (PhoneLog time-fallback). **Photos graph-searchable by TIME + LOCATION.**
- [x] **Checkpoint 3** (`2495b65`): `faces.py` (insightface detect+embed+cluster, LOCAL,
      no cloud) + `graph_faces.py` (`(FaceCluster)`/`(Person)`/`DEPICTS`/`IDENTIFIES`).
      Validated on 50-img sample (45 faces / 33 clusters). **Person half graph-ready.**

### What works now (query examples)
```cypher
// photos at "home" (South End) in 2025
MATCH (m:Media)-[:LOCATED_AT|:CAPTURED_AT_TIME]->(sp:SummaryPlace {place_role:'home'})
WHERE m.timestamp STARTS WITH '2025' RETURN m.path, m.timestamp

// after Scott labels a cluster: photos of Lexi
MATCH (p:Person {name:'Lexi'})<-[:IDENTIFIES]-(fc:FaceCluster)<-[:DEPICTS]-(m:Media)
RETURN m.path, m.timestamp
```

### Remaining (not blockers, documented)
- **Full face run**: `faces.py` on all 41k images is a multi-hour CPU job (insightface on
  CPU). Sample validated; trigger the full run when convenient (resumable via faces.done).
- **Cluster refinement**: greedy 0.5 threshold over-splits (33 clusters/45 faces on sample).
  Improve with two-pass centroid matching or HDBSCAN.
- **Human-in-loop labeling**: Scott renames `(Person {name:'Unknown N'})` → real name.
  One-time per cluster; downstream queries resolve automatically.
- **Video EXIF**: mov/mp4 get filename timestamps + null GPS (ffprobe not yet used).
- **SummaryPlace.name is NULL** on all nodes (geo pipeline gap) — join by coords, not name.
