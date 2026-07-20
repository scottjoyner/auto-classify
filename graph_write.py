#!/usr/bin/env python3
"""
graph_write.py — load media_inventory.jsonl into Neo4j as (Media) nodes,
linked to existing SummaryPlace nodes by LOCATION.

Strategies (reuse neo4j-summary-geo-clustering topology):
  1. LOCATED_AT       — Media has EXIF GPS: haversine-join to nearest SummaryPlace <=50km.
  2. CAPTURED_AT_TIME — no GPS: contemporaneous PhoneLog (±2h) -> its AT_PLACE
                         SummaryPlace. Resolved via an hour-bucket map built ONCE from
                         PhoneLog (18M rows) so we avoid per-record DB round-trips.

RUN ON HOST (x1-370). Env: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD.

Performance: all assignment computed in Python; graph writes use UNWIND batches
(500/tx). No per-record round-trips. Idempotent (MERGE on media_id / elementId).
"""
import os, sys, json, math, argparse
from datetime import datetime, timedelta
from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")
RADIUS_KM = 50.0
FALLBACK_HOURS = 2
BATCH = 500

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def load(path):
    rows, seen = [], set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["media_id"] in seen:
                continue
            seen.add(r["media_id"])
            rows.append(r)
    return rows

def hour_bucket(ts):
    """'2025-05-31 15:25:50' -> '2025-05-31T15' (UTC-ish hour key)."""
    try:
        iso = ts.replace(" ", "T") + "Z"
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H")
    except Exception:
        return None

def build_phonelog_hour_map(session):
    """ONE query: for each hour, the dominant AT_PLACE SummaryPlace.
    Returns {hour_key: elementId(place)}."""
    print("[graph] building PhoneLog hour->place map (one heavy query)...", flush=True)
    rows = session.execute_read(
        lambda tx: tx.run(
            "MATCH (pl:PhoneLog)-[:AT_PLACE]->(sp:SummaryPlace) "
            "WHERE pl.timestamp IS NOT NULL "
            "WITH pl, sp, datetime(pl.timestamp) AS dt "
            "WITH substring(pl.timestamp,0,13) AS hr, sp, count(*) AS c "
            "ORDER BY c DESC "
            "WITH hr, collect({eid: elementId(sp), c: c})[0] AS top "
            "RETURN hr AS hour_key, top.eid AS place_eid"
        ).data()
    )
    m = {r["hour_key"][:13]: r["place_eid"] for r in rows if r["place_eid"]}
    print(f"[graph] hour->place map: {len(m)} hours", flush=True)
    return m

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default="media_inventory.jsonl")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    rows = load(args.jsonl)
    if args.limit:
        rows = rows[:args.limit]
    print(f"[graph] {len(rows)} unique media", flush=True)

    driver = GraphDatabase.driver(URI, auth=(USER, PASS))
    with driver.session() as s:
        places = s.execute_read(
            lambda tx: tx.run(
                "MATCH (p:SummaryPlace) WHERE p.lat IS NOT NULL AND p.lon IS NOT NULL "
                "RETURN elementId(p) AS eid, p.lat AS lat, p.lon AS lon"
            ).data()
        )
        print(f"[graph] {len(places)} SummaryPlace for GPS join", flush=True)
        hour_map = build_phonelog_hour_map(s)

        # Precompute location assignment in Python
        located, timefb, noloc = [], [], []
        for r in rows:
            lat, lon = r["gps_lat"], r["gps_lon"]
            if lat is not None and lon is not None:
                best = None
                for p in places:
                    d = haversine(lat, lon, p["lat"], p["lon"])
                    if d <= RADIUS_KM and (best is None or d < best[1]):
                        best = (p["eid"], d)
                if best:
                    located.append((r["media_id"], best[0], round(best[1], 3)))
                    continue
            # fallback: hour bucket -> place
            hb = hour_bucket(r["timestamp"])
            if hb and hb in hour_map:
                timefb.append((r["media_id"], hour_map[hb]))
            else:
                noloc.append(r["media_id"])
        print(f"[graph] assignment: located={len(located)} timefb={len(timefb)} "
              f"noloc={len(noloc)}", flush=True)

        # Batch MERGE Media nodes
        def chunk(it, n=BATCH):
            for i in range(0, len(it), n):
                yield it[i:i+n]

        media_records = [
            {"mid": r["media_id"], "path": r["path"], "ext": r["ext"],
             "mt": r["media_type"], "ts": r["timestamp"], "w": r["width"],
             "h": r["height"], "lat": r["gps_lat"], "lon": r["gps_lon"],
             "size": r["size"]}
            for r in rows
        ]
        n = 0
        for ch in chunk(media_records):
            s.execute_write(
                lambda tx, ch=ch: tx.run(
                    "UNWIND $rows AS row "
                    "MERGE (m:Media {media_id: row.mid}) SET "
                    "m.path=row.path, m.ext=row.ext, m.media_type=row.mt, "
                    "m.timestamp=row.ts, m.width=row.w, m.height=row.h, "
                    "m.gps_lat=row.lat, m.gps_lon=row.lon, m.size=row.size, "
                    "m.ingested_at=datetime()",
                    rows=ch
                ).consume()
            )
            n += len(ch)
            if n % 5000 == 0:
                print(f"[graph] merged {n}/{len(media_records)} Media", flush=True)
        print(f"[graph] merged {n} Media nodes", flush=True)

        # Batch LOCATED_AT edges
        n = 0
        for ch in chunk(located):
            s.execute_write(
                lambda tx, ch=ch: tx.run(
                    "UNWIND $rows AS row "
                    "MATCH (m:Media {media_id: row.mid}), (p:SummaryPlace) "
                    "WHERE elementId(p)=row.eid "
                    "MERGE (m)-[:LOCATED_AT {distance_km: row.d, method:'exif_gps'}]->(p)",
                    rows=[{"mid": m, "eid": e, "d": d} for m, e, d in ch]
                ).consume()
            )
            n += len(ch)
        print(f"[graph] wrote {n} LOCATED_AT edges", flush=True)

        # Batch CAPTURED_AT_TIME edges
        n = 0
        for ch in chunk(timefb):
            s.execute_write(
                lambda tx, ch=ch: tx.run(
                    "UNWIND $rows AS row "
                    "MATCH (m:Media {media_id: row.mid}), (p:SummaryPlace) "
                    "WHERE elementId(p)=row.eid "
                    "MERGE (m)-[:CAPTURED_AT_TIME {method:'phonelog_time_fallback'}]->(p)",
                    rows=[{"mid": m, "eid": e} for m, e in ch]
                ).consume()
            )
            n += len(ch)
        print(f"[graph] wrote {n} CAPTURED_AT_TIME edges", flush=True)

    driver.close()
    print(f"[graph] DONE. located={len(located)} timefb={len(timefb)} noloc={len(noloc)}", flush=True)

if __name__ == "__main__":
    main()
