#!/bin/bash
# run_faces_npu.sh — Run face detection with Ryzen NPU acceleration
#
# Sets up the NPU environment and runs faces.py on the host.
# Usage:
#   ./run_faces_npu.sh [--limit 200] [--year 2025]
#   ./run_faces_npu.sh --photos-root /path/to/photos [--limit 100]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PHOTOS_ROOT="/media/scott/SSD_4TB/nextcloud_stage"
VENV="/media/scott/data/ryzen-ai-venv"

# NPU environment
export RYZEN_AI_INSTALLATION_PATH="$VENV"
export LD_PRELOAD="/opt/xilinx/xrt/lib/libxrt_coreutil.so.2 ${LD_PRELOAD:-}"
export LD_LIBRARY_PATH="${VENV}/lib/python3.12/site-packages/voe/lib:${VENV}/onnxruntime/lib:${VENV}/deployment/lib:/opt/xilinx/xrt/lib:${LD_LIBRARY_PATH:-}"

# Run
exec "$VENV/bin/python" "$SCRIPT_DIR/faces.py" \
    --photos-root "$PHOTOS_ROOT" \
    "$@"
