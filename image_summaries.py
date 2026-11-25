#!/usr/bin/env python3
"""Resumable local/private image summarization for the Nextcloud photo archive.

Required runtime configuration:
  IMAGE_SUMMARY_BASE_URL   OpenAI-compatible base URL, e.g. http://100.x.y.z:1234/v1
  IMAGE_SUMMARY_MODEL      Proven vision-capable model ID
  IMAGE_SUMMARY_VISION_VERIFIED=1

Optional:
  AUTO_CLASSIFY_PHOTOS_ROOT=/nas/nextcloud-primary-data/admin/files/Photos
  AUTO_CLASSIFY_STATE_DIR=/nas/.system/photo-pipeline

The script fails closed for public endpoints unless ALLOW_REMOTE_VISION=1 is
explicitly set. Missing/not-yet-migrated files are retried later and are never
marked done.
"""
import argparse
import base64
import io
import ipaddress
import json
import mimetypes
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image

LEGACY_PHOTOS_ROOT = "/var/www/html/data/admin/files/Photos"
PHOTOS_ROOT = os.environ.get("AUTO_CLASSIFY_PHOTOS_ROOT", LEGACY_PHOTOS_ROOT)
STATE_DIR = Path(os.environ.get(
    "AUTO_CLASSIFY_STATE_DIR",
    os.path.join(PHOTOS_ROOT, "_ingest"),
))
INVENTORY = STATE_DIR / "media_inventory.jsonl"
OUT = STATE_DIR / "image_summaries.jsonl"
DONE = STATE_DIR / "image_summaries.done"
ERRORS = STATE_DIR / "image_summaries.errors.jsonl"
BASE_URL = os.environ.get("IMAGE_SUMMARY_BASE_URL", "").rstrip("/")
MODEL = os.environ.get("IMAGE_SUMMARY_MODEL", "")
VERIFIED = os.environ.get("IMAGE_SUMMARY_VISION_VERIFIED") == "1"
ALLOW_REMOTE = os.environ.get("ALLOW_REMOTE_VISION") == "1"
PROMPT_VERSION = "photo-summary-v1"
PROMPT = (
    "Describe this personal photo factually in 1-3 concise sentences. "
    "Mention visible people only generically unless identity is explicit in the image; "
    "include the setting, salient objects/actions, readable text when useful, and "
    "uncertainty when something is unclear. Do not infer sensitive traits."
)

def endpoint_is_local_or_tailnet(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").rstrip(".").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        if host.endswith(".ts.net") or host.endswith(".lan"):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            addrs = {x[4][0] for x in socket.getaddrinfo(host, None)}
            return bool(addrs) and all(
                ipaddress.ip_address(a).is_loopback
                or ipaddress.ip_address(a).is_private
                or ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")
                for a in addrs
            )
    except Exception:
        return False

def resolve_path(raw: str) -> str:
    prefix = LEGACY_PHOTOS_ROOT.rstrip("/") + "/"
    if PHOTOS_ROOT != LEGACY_PHOTOS_ROOT and raw.startswith(prefix):
        return os.path.join(PHOTOS_ROOT, raw[len(prefix):])
    return raw

def load_inventory():
    with INVENTORY.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("media_type") == "image":
                yield r

def jpeg_data_url(path: str, max_px: int = 1280) -> str:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

def summarize(path: str) -> str:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 220,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": jpeg_data_url(path)}},
            ],
        }],
    }
    req = Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=120) as resp:
        body = json.load(resp)
    content = body["choices"][0]["message"].get("content", "")
    if isinstance(content, list):
        content = " ".join(
            str(x.get("text", "")) if isinstance(x, dict) else str(x)
            for x in content
        )
    content = str(content).strip()
    if not content:
        raise RuntimeError("vision model returned empty content")
    return content

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if not BASE_URL or not MODEL or not VERIFIED:
        print(
            "[image-summary] disabled: set IMAGE_SUMMARY_BASE_URL, "
            "IMAGE_SUMMARY_MODEL, and IMAGE_SUMMARY_VISION_VERIFIED=1 only "
            "after a live vision-capability check",
            file=sys.stderr,
        )
        return 2
    if not ALLOW_REMOTE and not endpoint_is_local_or_tailnet(BASE_URL):
        print("[image-summary] refusing non-local/non-tailnet endpoint", file=sys.stderr)
        return 2
    if args.self_check:
        print(json.dumps({
            "ok": True,
            "base_url": BASE_URL,
            "model": MODEL,
            "photos_root": PHOTOS_ROOT,
            "state_dir": str(STATE_DIR),
            "private_endpoint": endpoint_is_local_or_tailnet(BASE_URL),
        }, sort_keys=True))
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    done = set(DONE.read_text(errors="replace").splitlines()) if DONE.exists() else set()
    rows = [r for r in load_inventory() if str(r["media_id"]) not in done]
    if args.limit:
        rows = rows[:args.limit]
    print(f"[image-summary] {len(rows)} image(s) selected", flush=True)

    completed = failed = waiting = 0
    with OUT.open("a") as out, DONE.open("a") as donef, ERRORS.open("a") as errf:
        for idx, r in enumerate(rows, 1):
            mid = str(r["media_id"])
            path = resolve_path(str(r["path"]))
            if not os.path.exists(path):
                waiting += 1
                continue
            try:
                summary = summarize(path)
                rec = {
                    "media_id": mid,
                    "path": r["path"],
                    "summary": summary,
                    "model": MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                donef.write(mid + "\n")
                donef.flush()
                completed += 1
            except Exception as exc:
                failed += 1
                errf.write(json.dumps({
                    "media_id": mid,
                    "path": r["path"],
                    "error": f"{type(exc).__name__}: {exc}",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False) + "\n")
                errf.flush()
            if idx % 25 == 0:
                print(
                    f"[image-summary] progress={idx}/{len(rows)} "
                    f"completed={completed} failed={failed} waiting={waiting}",
                    flush=True,
                )
    print(
        f"[image-summary] done completed={completed} failed={failed} waiting={waiting}",
        flush=True,
    )
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
