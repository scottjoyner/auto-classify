#!/usr/bin/env python3
"""
status_ui.py — Pipeline status dashboard.

Serves at http://localhost:8082. Shows overall progress, cluster stats,
and recent activity.

Usage:
  python status_ui.py [--port 8082]
"""
import os, sys, json, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_config as config

PHOTOS_ROOT = config.PHOTOS_ROOT_HOST
INGEST_DIR = config.INGEST_DIR_HOST

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")


def get_stats():
    """Get pipeline statistics."""
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with d.session() as s:
        stats = {}
        stats["media"] = s.run("MATCH (m:Media) RETURN count(m) AS c").data()[0]["c"]
        stats["clusters"] = s.run("MATCH (fc:FaceCluster) RETURN count(fc) AS c").data()[0]["c"]
        stats["persons"] = s.run("MATCH (p:Person) RETURN count(p) AS c").data()[0]["c"]
        stats["labeled"] = s.run("MATCH (p:Person {labeled: true}) RETURN count(p) AS c").data()[0]["c"]
        stats["depicts"] = s.run("MATCH ()-[r:DEPICTS]->() RETURN count(r) AS c").data()[0]["c"]

        # Top clusters by size
        stats["top_clusters"] = s.run(
            "MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person) "
            "RETURN p.name AS name, fc.member_count AS count, fc.cluster_id AS cid "
            "ORDER BY fc.member_count DESC LIMIT 10"
        ).data()

        # Recently labeled
        stats["recent_labeled"] = s.run(
            "MATCH (p:Person {labeled: true})<-[:IDENTIFIES]-(fc:FaceCluster) "
            "RETURN p.name AS name, fc.member_count AS count "
            "ORDER BY p.name LIMIT 20"
        ).data()

    d.close()

    # File counts
    done_file = os.path.join(INGEST_DIR, "faces.done")
    stats["images_done"] = sum(1 for _ in open(done_file)) if os.path.exists(done_file) else 0

    # Count total images
    inv_file = os.path.join(INGEST_DIR, "media_inventory.jsonl")
    stats["total_images"] = 0
    if os.path.exists(inv_file):
        with open(inv_file) as f:
            for line in f:
                r = json.loads(line.strip())
                if r.get("media_type") == "image":
                    stats["total_images"] += 1

    return stats


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Pipeline Status</title>
<meta http-equiv="refresh" content="30">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f0f0f; color: #e0e0e0; padding: 20px; }
  h1 { color: #fff; margin-bottom: 8px; font-size: 24px; }
  .subtitle { color: #888; margin-bottom: 24px; font-size: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 16px; margin-bottom: 24px; }
  .card { background: #1a1a1a; padding: 20px; border-radius: 12px; border: 1px solid #333; }
  .card-num { font-size: 32px; font-weight: bold; color: #4fc3f7; }
  .card-label { font-size: 13px; color: #888; margin-top: 4px; }
  .progress-bar { width: 100%; height: 8px; background: #333; border-radius: 4px;
                  margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; background: #4fc3f7; border-radius: 4px;
                   transition: width 0.3s; }
  .section { margin-bottom: 24px; }
  .section-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #fff; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }
  th { color: #888; font-size: 12px; text-transform: uppercase; }
  td { font-size: 14px; }
  .labeled { color: #66bb6a; }
  .unlabeled { color: #888; }
  .nav { display: flex; gap: 12px; margin-bottom: 24px; }
  .nav a { color: #4fc3f7; text-decoration: none; padding: 8px 16px;
           background: #1a1a1a; border-radius: 8px; border: 1px solid #333; }
  .nav a:hover { background: #222; }
</style>
</head>
<body>
<h1>Pipeline Status</h1>
<p class="subtitle">Auto-refreshes every 30 seconds</p>

<div class="nav">
  <a href="http://localhost:8080">Label Faces</a>
  <a href="http://localhost:8081">Search Photos</a>
</div>

<div class="grid" id="stats"></div>

<div class="section">
  <div class="section-title">Face Detection Progress</div>
  <div id="progress"></div>
</div>

<div class="section">
  <div class="section-title">Top Clusters</div>
  <table id="top-clusters">
    <thead><tr><th>Name</th><th>Faces</th><th>Status</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">Labeled Persons</div>
  <table id="labeled">
    <thead><tr><th>Name</th><th>Faces</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
async function init() {
  const resp = await fetch('/api/stats');
  const s = await resp.json();

  const pct = s.total_images ? Math.round(s.images_done / s.total_images * 100) : 0;
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="card-num">${s.media.toLocaleString()}</div><div class="card-label">Media Nodes</div></div>
    <div class="card"><div class="card-num">${s.clusters}</div><div class="card-label">Face Clusters</div></div>
    <div class="card"><div class="card-num">${s.persons}</div><div class="card-label">Persons</div></div>
    <div class="card"><div class="card-num">${s.labeled}</div><div class="card-label">Labeled</div></div>
    <div class="card"><div class="card-num">${s.depicts}</div><div class="card-label">DEPICTS Edges</div></div>
  `;

  document.getElementById('progress').innerHTML = `
    <div style="color:#888;margin-bottom:8px;">${s.images_done.toLocaleString()} / ${s.total_images.toLocaleString()} images (${pct}%)</div>
    <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
  `;

  const topTbody = document.querySelector('#top-clusters tbody');
  topTbody.innerHTML = s.top_clusters.map(c => `
    <tr>
      <td class="${c.name.startsWith('Unknown') ? 'unlabeled' : 'labeled'}">${c.name}</td>
      <td>${c.count}</td>
      <td>${c.name.startsWith('Unknown') ? 'Unlabeled' : 'Labeled'}</td>
    </tr>
  `).join('');

  const labTbody = document.querySelector('#labeled tbody');
  labTbody.innerHTML = s.recent_labeled.map(c => `
    <tr><td>${c.name}</td><td>${c.count}</td></tr>
  `).join('');
}

init();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())
        elif parsed.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            stats = get_stats()
            self.wfile.write(json.dumps(stats).encode())
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    args = ap.parse_args()

    print(f"[status-ui] serving at http://localhost:{args.port}")

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[status-ui] stopped")


if __name__ == "__main__":
    main()
