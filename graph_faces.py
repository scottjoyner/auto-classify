#!/usr/bin/env python3
"""
graph_faces.py — load face_embeddings.jsonl + face_clusters.jsonl into Neo4j.

Creates:
  (FaceCluster {cluster_id, member_count})
  (Media)-[:DEPICTS {bbox}]->(FaceCluster)
  (Person {name:'Unknown <cid>'}) <- (FaceCluster)-[:IDENTIFIES]->(Person)

The (Person) is a placeholder; Scott labels a cluster by renaming its Person node
(e.g. `MATCH (p:Person {name:'Unknown 0'}) SET p.name='Lexi'`). After labeling,
"photos of Lexi" = (p:Person {name:'Lexi'})<-[:IDENTIFIES]-(fc:FaceCluster)<-[:DEPICTS]-(m:Media).

RUN ON HOST (x1-370). Env: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD.
Idempotent (MERGE on cluster_id / media_id).
"""
import os, sys, json, argparse
from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")
BATCH = 500

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb", default="face_embeddings.jsonl")
    ap.add_argument("--clusters", default="face_clusters.jsonl")
    args = ap.parse_args()

    # load clusters
    clusters = {}
    with open(args.clusters) as f:
        for line in f:
            line = line.strip()
            if line:
                c = json.loads(line)
                clusters[c["cluster_id"]] = c["member_count"]

    # load embeddings (media_id, face_idx, bbox, cluster_id)
    faces = []
    with open(args.emb) as f:
        for line in f:
            line = line.strip()
            if line:
                faces.append(json.loads(line))

    print(f"[faces-graph] {len(clusters)} clusters, {len(faces)} face detections", flush=True)

    driver = GraphDatabase.driver(URI, auth=(USER, PASS))
    with driver.session() as s:
        # MERGE FaceCluster + placeholder Person + IDENTIFIES
        n = 0
        for ch in [list(clusters.items())[i:i+BATCH] for i in range(0, len(clusters), BATCH)]:
            s.execute_write(
                lambda tx, ch=ch: tx.run(
                    "UNWIND $rows AS row "
                    "MERGE (fc:FaceCluster {cluster_id: row.cid}) "
                    "SET fc.member_count = row.mc "
                    "MERGE (p:Person {cluster_id: row.cid}) "
                    "SET p.name = 'Unknown ' + row.cid, p.labeled = false "
                    "MERGE (fc)-[:IDENTIFIES]->(p)",
                    rows=[{"cid": cid, "mc": mc} for cid, mc in ch]
                ).consume()
            )
            n += len(ch)
        print(f"[faces-graph] merged {n} FaceCluster + Person nodes", flush=True)

        # MERGE Media-[:DEPICTS]->FaceCluster
        n = 0
        for ch in [faces[i:i+BATCH] for i in range(0, len(faces), BATCH)]:
            s.execute_write(
                lambda tx, ch=ch: tx.run(
                    "UNWIND $rows AS row "
                    "MATCH (m:Media {media_id: row.mid}), (fc:FaceCluster {cluster_id: row.cid}) "
                    "MERGE (m)-[:DEPICTS {bbox: row.bbox, face_idx: row.fi}]->(fc)",
                    rows=[{"mid": f["media_id"], "cid": f["cluster_id"],
                           "bbox": f["bbox"], "fi": f["face_idx"]} for f in ch]
                ).consume()
            )
            n += len(ch)
        print(f"[faces-graph] wrote {n} DEPICTS edges", flush=True)

    driver.close()
    print("[faces-graph] DONE", flush=True)

if __name__ == "__main__":
    main()
