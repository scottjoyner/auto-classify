# auto-classify

<!-- repo-entrypoint-guide:start -->
> **Start here:** The workflow indexes authorized photo/video archives into metadata and face embeddings before writing to Neo4j. Read [What it does](#what-it-does), [Quick start](#quick-start), and [Output files](#output-files) in that order.
> 
> **Privacy and reproducibility:** Face labeling and location data are sensitive. Test against an approved subset, keep raw media and embeddings out of public commits, and validate graph ownership and deduplication before enabling batch writes.
<!-- repo-entrypoint-guide:end -->


Ingest photo/video archives into Neo4j so media becomes graph-searchable by
**time**, **location**, and **person**. Designed for the Nextcloud `Photos/`
iOS instant-upload feed (~117GB, ~41k files, 2007–2026).

## What it does

```
Photos/ ──► extract_metadata.py ──► media_inventory.jsonl ──► graph_write.py ──► Neo4j (Media + Location)
                  │
                  ▼
              faces.py ──► face_embeddings.jsonl ──► graph_faces.py ──► Neo4j (Person + DEPICTS)
                           face_clusters.jsonl
```

- **Time**: EXIF `DateTimeOriginal` (92% coverage) or filename parse for videos
- **Location**: EXIF GPS (86% coverage) joined by haversine to `SummaryPlace` nodes (≤50km)
- **Person**: `insightface` local face detect → 512-d embedding → greedy cluster → you label

Then: *"photos of Lexi at the beach in June 2025"* is a one-hop Cypher query.

## Hardware

| Component | Spec |
|-----------|------|
| CPU | AMD Ryzen AI Max Pro 390 (24 threads) |
| NPU | XDNA NPU via VitisAI Execution Provider |
| RAM | 24GB |
| Storage | `/media/scott/SSD_4TB/` (NVMe) |
| Photos | `/media/scott/SSD_4TB/nextcloud_stage/` |

## Quick start

```bash
# 1. Generate media inventory
python extract_metadata.py --photos-root /media/scott/SSD_4TB/nextcloud_stage

# 2. Write to Neo4j
python graph_write.py --jsonl /media/scott/SSD_4TB/nextcloud_stage/_ingest/media_inventory.jsonl

# 3. Run face detection (NPU-accelerated)
./run_faces_npu.sh --limit 200

# 4. Load faces into Neo4j
python graph_faces.py \
  --emb /media/scott/SSD_4TB/nextcloud_stage/_ingest/face_embeddings.jsonl \
  --clusters /media/scott/SSD_4TB/nextcloud_stage/_ingest/face_clusters.jsonl

# 5. Label faces in the web UI
python classify_ui.py
# Open http://localhost:8080
```

## Scripts

| Script | Purpose | Runs on |
|--------|---------|---------|
| `extract_metadata.py` | Scan photos, extract EXIF → JSONL | Host or container |
| `graph_write.py` | MERGE Media nodes + GPS→SummaryPlace join | Host (Neo4j) |
| `faces.py` | insightface detect/embed/cluster | Host (NPU) or container |
| `graph_faces.py` | MERGE FaceCluster/Person + DEPICTS edges | Host (Neo4j) |
| `classify_ui.py` | Web UI for labeling face clusters | Host |
| `batchrunner.py` | Idle-gated batch dispatcher | Host |
| `run_faces_npu.sh` | NPU environment wrapper | Host |

## Output files

All written to `_ingest/` under the photos root:

| File | Format | Description |
|------|--------|-------------|
| `media_inventory.jsonl` | `{path, media_id, ext, size, media_type, timestamp, width, height, gps_lat, gps_lon}` | One record per file |
| `face_embeddings.jsonl` | `{media_id, face_idx, bbox, cluster_id, embedding[512]}` | One record per face detected |
| `face_clusters.jsonl` | `{cluster_id, member_count}` | Cluster summary |
| `faces.done` | media_id per line | Resumability checkpoint |

## NPU acceleration

Face detection runs on the Ryzen XDNA NPU via the VitisAI Execution Provider:

- First run compiles the AIE firmware (~2-3 min, cached at `/tmp/scott/vaip/.cache/`)
- Subsequent runs use the cached compiled model
- If the VitisAI EP fails (e.g. model incompatibility), automatically falls back to CPU
- The NPU probe runs in a subprocess to avoid C++ abort crashes

```bash
# The wrapper script sets all required env vars
./run_faces_npu.sh --limit 200

# Or set manually
export RYZEN_AI_INSTALLATION_PATH=/media/scott/data/ryzen-ai-venv
export LD_PRELOAD="/opt/xilinx/xrt/lib/libxrt_coreutil.so.2"
export LD_LIBRARY_PATH=".../voe/lib:.../onnxruntime/lib:.../deployment/lib:/opt/xilinx/xrt/lib"
```

## Batch processing

The `batchrunner.py` drives face detection in safe, resumable slices:

```bash
# Check what would run
python batchrunner.py dispatch --dry-run

# Run one slice (200 images) now
python batchrunner.py --force faces_full --execute

# Check progress
python batchrunner.py status
```

Configuration in `batch_jobs/faces_full.yaml`:
- **Slice size**: 200 images per tick
- **Idle gate**: host load < 2.0 per CPU
- **Allowed hours**: 0-7, 19-23 (nights)
- **Priority**: batch (5) — lowest, never preempts

## Cypher queries

```cypher
// All Media nodes
MATCH (m:Media) RETURN count(m)

// Photos at home in 2025
MATCH (m:Media)-[:LOCATED_AT|:CAPTURED_AT_TIME]->(sp:SummaryPlace {place_role:'home'})
WHERE m.timestamp STARTS WITH '2025'
RETURN m.path, m.timestamp

// Biggest face clusters (most common people)
MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person)
RETURN p.name, fc.member_count
ORDER BY fc.member_count DESC LIMIT 20

// Photos of a labeled person
MATCH (p:Person {name:'Lexi'})<-[:IDENTIFIES]-(fc:FaceCluster)<-[:DEPICTS]-(m:Media)
RETURN m.path, m.timestamp

// Unlabeled clusters (need your input)
MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person)
WHERE p.labeled = false
RETURN p.name, fc.member_count
ORDER BY fc.member_count DESC

// Photos with most faces (group shots)
MATCH (m:Media)-[r:DEPICTS]->(fc:FaceCluster)
RETURN m.path, count(r) AS faces
ORDER BY faces DESC LIMIT 10

// Find similar faces by embedding cosine similarity
MATCH (fc:FaceCluster)
RETURN fc.cluster_id, fc.member_count
ORDER BY fc.member_count DESC
```

## Face classification UI

```bash
python classify_ui.py
# Opens http://localhost:8080
```

The web UI shows face clusters sorted by size. For each cluster:
- View all face thumbnails from that cluster
- Enter a name to label the person
- Skip to next cluster
- Merge clusters if they're the same person

Labels update Neo4j instantly — queries immediately reflect the new names.

## Project structure

```
auto-classify/
├── ac_config.py              # Constants and paths
├── extract_metadata.py       # EXIF extraction → media_inventory.jsonl
├── faces.py                  # Face detect/embed/cluster → JSONL
├── graph_write.py            # Media nodes → Neo4j
├── graph_faces.py            # Face/Person nodes → Neo4j
├── classify_ui.py            # Web UI for labeling
├── batchrunner.py            # Idle-gated batch dispatcher
├── run_faces_npu.sh          # NPU environment wrapper
├── requirements.txt          # Host-side Python deps
├── batch_jobs/
│   ├── faces_full.yaml       # Full face detection manifest
│   └── auto_ingest.yaml      # Auto-ingest service manifest
└── _ingest/                  # Generated output (on photos drive)
    ├── media_inventory.jsonl
    ├── face_embeddings.jsonl
    ├── face_clusters.jsonl
    └── faces.done
```
