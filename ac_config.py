# auto-classify — Nextcloud photo/video ingestion into Neo4j
#
# Makes Scott's Nextcloud media (Photos/, 117GB, ~41k files) graph-searchable by
# TIME (EXIF timestamp), LOCATION (EXIF GPS -> existing SummaryPlace nodes), and
# PERSON (insightface face detect/embed -> cluster -> Scott labels -> Person).
#
# Pipeline stages (each commits a checkpoint):
#   extract_metadata.py  - scan Photos/, pull EXIF (timestamp+GPS+dimensions),
#                          write media_inventory.jsonl  [RUN INSIDE nextcloud container]
#   graph_write.py       - MERGE (Media) nodes + GPS->SummaryPlace distance join
#                          + time-fallback to PhoneLog GPS             [RUN ON HOST]
#   faces.py             - insightface detect+embed+cluster -> face_clusters.jsonl
#                          [RUN INSIDE nextcloud container]
#   graph_faces.py       - MERGE (Person)/(FaceCluster) + DEPICTS edges [RUN ON HOST]
#
# Verified facts (2026-07-20):
#   - Photos store (from x1-370): nextcloud container /var/www/html/data/admin/files/Photos
#     = docker volume docker-compose_nextcloud-data. Host fs shows it empty (root-owned
#     graph store); access ONLY via `docker exec nextcloud` (root). 
#   - File types: 29453 jpg, 9470 png, 1386 mov, 980 mp4, 57 jpeg. NO heic.
#   - EXIF sample (52 files 2007/2015/2025): 92% have DateTimeOriginal (Exif sub-IFD
#     0x8769 tag 36867), 86% have GPS. GPS coverage is HIGH -> exact coord join viable.
#   - SummaryPlace schema: name(NULL), lat, lon, place_role, summary_count. Join by
#     coordinate proximity (haversine <=50km), NOT by name.
#   - Pillow 12 returns IFDRational for GPS; cast float(). DateTimeOriginal is in the
#     Exif SUB-IFD (0x8769), not top-level.
#   - insightface pulls opencv-python (needs libGL); use opencv-python-headless.
#   - Container python is 3.13 (Debian 13). Host venv (3.12) binaries DON'T load in
#     container; build the venv INSIDE the container instead.
#
# Execution model:
#   - extract_metadata.py / faces.py run INSIDE the nextcloud container
#     (docker exec nextcloud /tmp/facevenv/bin/python /work/extract_metadata.py)
#     because only the container can read the photo volume. They write JSONL to
#     /var/www/html/data/admin/files/Photos/_ingest/ (retrieved via docker exec cat).
#   - graph_write.py / graph_faces.py run ON HOST (x1-370) where Neo4j Bolt
#     (100.64.43.123:7687) + git are reachable.
#
# Env (host graph_write): NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

PHOTOS_ROOT_IN_CONTAINER = "/var/www/html/data/admin/files/Photos"
INGEST_DIR_IN_CONTAINER = "/var/www/html/data/admin/files/Photos/_ingest"

# media extensions we handle
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS

# GPS join radius (km) to existing SummaryPlace — same as PhoneLog pipeline
GPS_JOIN_RADIUS_KM = 50
# time fallback window (hours) for PhoneLog GPS when photo has no GPS
TIME_FALLBACK_HOURS = 2
