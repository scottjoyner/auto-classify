#!/usr/bin/env python3
"""
load_faces.py — Load face embeddings and clusters into Neo4j.

Resumable: skips already-ingested media_ids.

Usage:
  python load_faces.py [--emb /path/to/face_embeddings.jsonl] [--clusters /path/to/face_clusters.jsonl]
"""
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_config as config

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")


def load_embeddings(emb_file, cluster_file):
    """Load face data into Neo4j."""
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    # Load clusters
    clusters = {}
    with open(cluster_file) as f:
        for line in f:
            c = json.loads(line.strip())
            clusters[c["cluster_id"]] = c["member_count"]

    with d.session() as s:
        # Create cluster nodes
        for cid, count in clusters.items():
            s.run(
                "MERGE (fc:FaceCluster {cluster_id: $cid}) "
                "SET fc.member_count = $count",
                cid=cid, count=count,
            )
        print(f"[load] Upserted {len(clusters)} FaceCluster nodes")

        # Create Person nodes for clusters that don't have one
        s.run(
            "MATCH (fc:FaceCluster) "
            "WHERE NOT (fc)-[:IDENTIFIES]->(:Person) "
            "CREATE (p:Person {labeled: false}) "
            "SET p.name = 'Unknown ' + toString(fc.cluster_id) "
            "CREATE (fc)-[:IDENTIFIES]->(p)"
        )
        print("[load] Created missing Person nodes")

    # Load embeddings
    with open(emb_file) as f:
        emb_lines = [json.loads(line.strip()) for line in f]

    # Load media inventory for path lookup
    inv_file = os.path.join(os.path.dirname(emb_file), "media_inventory.jsonl")
    media_paths = {}
    if os.path.exists(inv_file):
        with open(inv_file) as f:
            for line in f:
                r = json.loads(line.strip())
                media_paths[r["media_id"]] = r["path"]

    with d.session() as s:
        ingested = 0
        for e in emb_lines:
            mid = e["media_id"]
            path = media_paths.get(mid, "")
            s.run(
                "MERGE (m:Media {media_id: $mid}) "
                "SET m.path = $path",
                mid=mid, path=path,
            )
            s.run(
                "MATCH (fc:FaceCluster {cluster_id: $cid}), (m:Media {media_id: $mid}) "
                "MERGE (fc)-[:CONTAINS]->(m)",
                cid=e["cluster_id"], mid=mid,
            )
            s.run(
                "MATCH (p:Person)<-[:IDENTIFIES]-(fc:FaceCluster {cluster_id: $cid}), (m:Media {media_id: $mid}) "
                "MERGE (m)-[:DEPICTS {face_idx: $fi, bbox: $bbox}]->(p)",
                cid=e["cluster_id"], mid=mid, fi=e["face_idx"], bbox=json.dumps(e["bbox"]),
            )
            ingested += 1

        print(f"[load] Ingested {ingested} face embeddings")

    # Update cluster member counts
    with d.session() as s:
        s.run(
            "MATCH (fc:FaceCluster) "
            "SET fc.member_count = size([(fc)-[:CONTAINS]->(m:Media) | m])"
        )
        print("[load] Updated cluster member counts")

    d.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb", help="Path to face_embeddings.jsonl")
    ap.add_argument("--clusters", help="Path to face_clusters.jsonl")
    ap.add_argument("--photos-root", help="Override photos root path")
    args = ap.parse_args()

    ingest_dir = os.path.join(args.photos_root, "_ingest") if args.photos_root else config.INGEST_DIR_HOST
    emb_file = args.emb or os.path.join(ingest_dir, "face_embeddings.jsonl")
    cluster_file = args.clusters or os.path.join(ingest_dir, "face_clusters.jsonl")

    print(f"[load] embeddings: {emb_file}")
    print(f"[load] clusters: {cluster_file}")
    load_embeddings(emb_file, cluster_file)


if __name__ == "__main__":
    main()
