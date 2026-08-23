# Onboarding Guide

Step-by-step guide to get auto-classify running on a new machine.

## Prerequisites

- Ubuntu 24.04+ (tested on 24.04 with kernel 6.17+)
- Python 3.12+
- AMD Ryzen AI Max Pro 390 with XDNA NPU (or CPU-only fallback)
- Neo4j instance (Bolt protocol)
- Photo storage mounted at a known path

## Step 1: Clone and enter the repo

```bash
cd ~/git
git clone <repo-url> auto-classify
cd auto-classify
```

## Step 2: Set up the Python environment

```bash
# Create venv (use the ryzen-ai-venv if NPU is available)
python3 -m venv venv
source venv/bin/activate

# Install host-side dependencies
pip install -r requirements.txt
pip install Pillow insightface opencv-python-headless numpy
```

### If you have the Ryzen NPU

The NPU venv is at `/media/scott/data/ryzen-ai-venv/` with pre-installed:
- `onnxruntime-vitisai` (VitisAI EP for NPU)
- `voe` (Vitis ONNX Execution passes)
- `insightface`, `opencv-python-headless`, `pillow`, `numpy`
- `neo4j`, `pyyaml`

Use it directly:
```bash
export VENV=/media/scott/data/ryzen-ai-venv
$VENV/bin/python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# Should show: ['VitisAIExecutionProvider', 'CPUExecutionProvider']
```

### CPU-only fallback

If NPU is not available, the code auto-detects and uses `CPUExecutionProvider`.
Face detection will be ~10x slower but functionally identical.

## Step 3: Verify NPU (optional)

```bash
# Test NPU hardware
xrt-smi examine

# Test VitisAI EP
$VENV/bin/python -c "
import onnxruntime as ort
print(ort.get_available_providers())
assert 'VitisAIExecutionProvider' in ort.get_available_providers()
print('NPU OK')
"
```

### Troubleshooting NPU

If `VitisAIExecutionProvider` is not in the providers list:

1. Check the pybind module isn't shadowed:
   ```bash
   ls $VENV/lib/python3.12/site-packages/onnxruntime/capi/onnxruntime_pybind11_state*
   # The .so (not .cpython-*.so) should be ~30MB (VitisAI-enabled)
   # If you see a ~25MB .cpython-*.so, rename it:
   mv $VENV/lib/python3.12/site-packages/onnxruntime/capi/onnxruntime_pybind11_state.cpython-312-x86_64-linux-gnu.so \
      $VENV/lib/python3.12/site-packages/onnxruntime/capi/onnxruntime_pybind11_state.cpython-312-x86_64-linux-gnu.so.bak
   ```

2. Set required env vars:
   ```bash
   export LD_PRELOAD="/opt/xilinx/xrt/lib/libxrt_coreutil.so.2"
   export LD_LIBRARY_PATH="$VENV/lib/python3.12/site-packages/voe/lib:$VENV/onnxruntime/lib:$VENV/deployment/lib:/opt/xilinx/xrt/lib"
   export RYZEN_AI_INSTALLATION_PATH=$VENV
   ```

## Step 4: Configure paths

Edit `ac_config.py` or set paths via CLI args:

```bash
# Where are the photos?
PHOTOS_ROOT="/media/scott/SSD_4TB/nextcloud_stage"

# Where is Neo4j?
export NEO4J_URI="bolt://100.64.43.123:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="knowledge_graph_2026"
```

## Step 5: Generate media inventory

```bash
python extract_metadata.py --photos-root $PHOTOS_ROOT
# Output: $PHOTOS_ROOT/_ingest/media_inventory.jsonl
```

This scans all photos, reads EXIF data (timestamp, GPS, dimensions), and writes
one JSON record per file. Takes ~6 minutes for 10k files.

## Step 6: Write Media nodes to Neo4j

```bash
python graph_write.py --jsonl $PHOTOS_ROOT/_ingest/media_inventory.jsonl
# Creates: (Media) nodes + LOCATED_AT + CAPTURED_AT_TIME edges
```

## Step 7: Run face detection

```bash
# NPU-accelerated (recommended)
./run_faces_npu.sh --limit 200

# Or directly
python faces.py --photos-root $PHOTOS_ROOT --limit 200
```

This is resumable — re-running skips already-processed images (tracked in `faces.done`).

## Step 8: Load faces into Neo4j

```bash
python graph_faces.py \
  --emb $PHOTOS_ROOT/_ingest/face_embeddings.jsonl \
  --clusters $PHOTOS_ROOT/_ingest/face_clusters.jsonl
```

## Step 9: Label faces

```bash
python classify_ui.py
# Open http://localhost:8080
```

The UI shows face clusters sorted by size (most common people first).
For each cluster:
1. View the face thumbnails
2. Type a name and press Enter to label
3. Or click Skip to move to the next cluster

Labels update Neo4j instantly.

## Step 10: Query your photos

```cypher
// Open Neo4j Browser at http://localhost:7474

// Photos of Lexi
MATCH (p:Person {name:'Lexi'})<-[:IDENTIFIES]-(fc)<-[:DEPICTS]-(m:Media)
RETURN m.path, m.timestamp

// Unlabeled people (need your input)
MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person)
WHERE p.labeled = false
RETURN p.name, fc.member_count
ORDER BY fc.member_count DESC
```

## Running batch processing

For the full 10k+ image run:

```bash
# Option A: Manual slices
while true; do
  python faces.py --photos-root $PHOTOS_ROOT --limit 200
  # Check progress
  wc -l $PHOTOS_ROOT/_ingest/faces.done
done

# Option B: Batchrunner (idle-gated, respects allowed hours)
python batchrunner.py dispatch          # one slice when idle
python batchrunner.py status            # check progress
python batchrunner.py dispatch --dry-run  # preview decisions
```

## Verifying the pipeline

```bash
# Quick health check
python -c "
from neo4j import GraphDatabase
d = GraphDatabase.driver('bolt://100.64.43.123:7687', auth=('neo4j', 'knowledge_graph_2026'))
with d.session() as s:
    m = s.run('MATCH (m:Media) RETURN count(m)').data()[0]['count']
    fc = s.run('MATCH (fc:FaceCluster) RETURN count(fc)').data()[0]['count']
    p = s.run('MATCH (p:Person) RETURN count(p)').data()[0]['count']
    dep = s.run('MATCH ()-[r:DEPICTS]->() RETURN count(r)').data()[0]['count']
    print(f'Media: {m}, Clusters: {fc}, Persons: {p}, DEPICTS: {dep}')
d.close()
"
```
