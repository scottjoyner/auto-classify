#!/usr/bin/env python3
"""
extract_metadata.py — scan Nextcloud Photos/, extract EXIF metadata, write JSONL.

RUN INSIDE the nextcloud container (it's the only place that can read the photo
volume). Reads config.PHOTOS_ROOT_IN_CONTAINER, writes JSONL to
config.INGEST_DIR_IN_CONTAINER/media_inventory.jsonl.

Idempotent: tracks processed sha256 in a sidecar .done set; re-running skips them.
Resume-safe across crashes.

Usage:
  docker exec nextcloud /tmp/facevenv/bin/python /work/extract_metadata.py [--year 2025] [--limit 100]
"""
import os, sys, json, hashlib, glob, argparse, traceback
sys.path.insert(0, "/work")
from ac_config import (PHOTOS_ROOT_IN_CONTAINER, INGEST_DIR_IN_CONTAINER,
                    IMAGE_EXTS, VIDEO_EXTS, ALL_EXTS)

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

OUT_DIR = INGEST_DIR_IN_CONTAINER
OUT_FILE = os.path.join(OUT_DIR, "media_inventory.jsonl")
DONE_FILE = os.path.join(OUT_DIR, "media_inventory.done")

def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def exif_datetime(ex):
    """Return ISO-ish 'YYYY-MM-DD HH:MM:SS' from EXIF, or None."""
    eifd = ex.get_ifd(0x8769)  # Exif sub-IFD
    for tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized
        v = eifd.get(tag)
        if v:
            # format 'YYYY:MM:DD HH:MM:SS'
            s = str(v).replace(":", "-", 2)
            return s
    top = ex.get(306)  # DateTime (top-level)
    if top:
        return str(top).replace(":", "-", 2)
    return None

def exif_gps(ex):
    """Return (lat, lon) signed floats, or None."""
    g = ex.get_ifd(34853)  # GPS IFD
    if not (g.get(2) and g.get(4)):
        return None
    def r(v):
        try:
            d = float(v[0]); m = float(v[1]); s = float(v[2])
            return d + m / 60.0 + s / 3600.0
        except Exception:
            return None
    lat = r(g[2]); lon = r(g[4])
    if lat is None or lon is None:
        return None
    lat = -lat if g.get(1) == "S" else lat
    lon = -lon if g.get(3) == "W" else lon
    return (round(lat, 6), round(lon, 6))

def filename_datetime(path):
    """iPhone export naming: '25-05-01 16-33-13 6945.jpg' -> parse prefix."""
    base = os.path.basename(path)
    parts = base.replace("_", " ").split(" ")
    # try 'YY-MM-DD HH-MM-SS'
    try:
        date_part, time_part = parts[0], parts[1]
        yy, mm, dd = date_part.split("-")
        hh, mi, ss = time_part.split("-")
        year = int(yy) + (2000 if int(yy) < 70 else 1900)
        return f"{year:04d}-{mm}-{dd} {hh}:{mi}:{ss}"
    except Exception:
        return None

def process(path):
    rec = {
        "path": path,
        "media_id": sha256(path),
        "ext": os.path.splitext(path)[1].lower(),
        "size": os.path.getsize(path),
    }
    ext = rec["ext"]
    rec["media_type"] = "video" if ext in VIDEO_EXTS else "image"
    # timestamp: EXIF for images, filename for videos / fallback
    dt = None
    w = h = None
    gps = None
    if ext in IMAGE_EXTS:
        try:
            img = Image.open(path)
            w, h = img.size
            ex = img.getexif()
            dt = exif_datetime(ex)
            gps = exif_gps(ex)
        except Exception as e:
            rec["error"] = f"image_exif: {e}"
    if dt is None:
        dt = filename_datetime(path)
    rec["timestamp"] = dt
    rec["width"], rec["height"] = w, h
    rec["gps_lat"], rec["gps_lon"] = (gps if gps else (None, None))
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", help="only this year dir")
    ap.add_argument("--limit", type=int, help="max files (for testing)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(l.strip() for l in f if l.strip())

    # gather files
    root = PHOTOS_ROOT_IN_CONTAINER
    pattern = os.path.join(root, args.year, "**", "*") if args.year else os.path.join(root, "**", "*")
    files = [p for p in glob.glob(pattern, recursive=True)
             if os.path.isfile(p) and os.path.splitext(p)[1].lower() in ALL_EXTS
             and p not in done]
    if args.limit:
        files = files[:args.limit]
    print(f"[extract] {len(files)} files to process (year={args.year or 'ALL'})", flush=True)

    n = 0
    with open(OUT_FILE, "a") as out, open(DONE_FILE, "a") as donef:
        for i, p in enumerate(files, 1):
            try:
                rec = process(p)
                out.write(json.dumps(rec) + "\n")
                donef.write(p + "\n")
                n += 1
            except Exception as e:
                print(f"[extract] ERR {p}: {e}", file=sys.stderr, flush=True)
            if i % 200 == 0:
                print(f"[extract] {i}/{len(files)} processed", flush=True)
    print(f"[extract] DONE: wrote {n} records to {OUT_FILE}", flush=True)

if __name__ == "__main__":
    main()
