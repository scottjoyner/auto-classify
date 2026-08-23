# Plan: auto-classify Face Detection with Ryzen NPU

## Goal
Run the auto-classify face detection pipeline on photos at `/media/scott/SSD_4TB/nextcloud_stage/` using the Ryzen AI Max Pro 390 NPU via the VitisAI Execution Provider.

## Current State
- **NPU working**: VitisAI EP confirmed (556 it/s, 1.8ms/iter on test model)
- **venv**: `/media/scott/data/ryzen-ai-venv/` has onnxruntime-vitisai, voe, insightface, opencv-python-headless, pillow, numpy
- **Photos**: `/media/scott/SSD_4TB/nextcloud_stage/` — 41k files (jpg/png/mov/mp4)
- **Problem**: Current code only runs inside Docker container (nextcloud) on CPU; NPU is only accessible on the host

## Changes

### 1. `ac_config.py` — Add host-side paths
Add constants for running on the host:
```python
PHOTOS_ROOT_HOST = "/media/scott/SSD_4TB/nextcloud_stage"
INGEST_DIR_HOST = "/media/scott/SSD_4TB/nextcloud_stage/_ingest"
```

### 2. `faces.py` — NPU provider detection + host-side mode
- Add `--photos-root` arg (defaults to container path, override with host path)
- Replace hardcoded `providers=["CPUExecutionProvider"]` with auto-detection:
  - Try `VitisAIExecutionProvider` first
  - Fall back to `CPUExecutionProvider`
- Add `--output-dir` arg for JSONL output location (default: `_ingest/` under photos root)
- Set env vars needed for VitisAI EP (`LD_LIBRARY_PATH`, `LD_PRELOAD`, `RYZEN_AI_INSTALLATION_PATH`)
- Register the VitisAI EP library dynamically if available

### 3. `extract_metadata.py` — Host-side mode
- Add `--photos-root` arg to scan the network drive directly (not container path)
- Add `--output-dir` arg for JSONL output
- Keep existing container mode as default

### 4. `batch_jobs/faces_full.yaml` — Update for host-side execution
- Change command from `docker exec nextcloud ...` to direct Python execution on host
- Use the ryzen-ai-venv Python
- Set environment variables for NPU
- Update paths to use `/media/scott/SSD_4TB/nextcloud_stage/`

### 5. Create `run_faces_npu.sh` — Wrapper script
A shell script that:
- Sets `LD_PRELOAD`, `LD_LIBRARY_PATH`, `RYZEN_AI_INSTALLATION_PATH`
- Activates the ryzen-ai-venv
- Runs faces.py with correct args
- Can be called by batchrunner or directly

## Execution Order
1. Generate `media_inventory.jsonl` from network drive (one-time, ~10 min)
2. Run face detection with NPU (resumable, ~205 slices of 200 images)
3. Each slice writes to `_ingest/face_embeddings.jsonl` + `_ingest/faces.done`
4. Run `graph_faces.py` to load results into Neo4j

## Verification
1. Run `extract_metadata.py --photos-root /media/scott/SSD_4TB/nextcloud_stage --limit 10` to test metadata extraction
2. Run `faces.py --photos-root /media/scott/SSD_4TB/nextcloud_stage --limit 5` to test NPU face detection
3. Verify output files are created and contain valid data
4. Run full pipeline and monitor batchrunner status
