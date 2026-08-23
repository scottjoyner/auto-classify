#!/usr/bin/env python3
"""
search_ui.py — Search photos by person name.

Serves at http://localhost:8081. Shows face clusters with labels,
lets you search for a person and see all their photos.

Usage:
  python search_ui.py [--port 8081] [--photos-root /path/to/photos]
"""
import os, sys, json, argparse, base64, io
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_config as config

PHOTOS_ROOT = config.PHOTOS_ROOT_HOST
INGEST_DIR = config.INGEST_DIR_HOST
EMB_FILE = os.path.join(INGEST_DIR, "face_embeddings.jsonl")
CLUSTER_FILE = os.path.join(INGEST_DIR, "face_clusters.jsonl")
INV_FILE = os.path.join(INGEST_DIR, "media_inventory.jsonl")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")


def load_media_index():
    """Build a media_id -> path index."""
    idx = {}
    if os.path.exists(INV_FILE):
        with open(INV_FILE) as f:
            for line in f:
                r = json.loads(line.strip())
                idx[r["media_id"]] = r["path"]
    return idx


def get_all_persons():
    """Get all persons from Neo4j with their clusters."""
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with d.session() as s:
        rows = s.run(
            "MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person) "
            "RETURN p.name AS name, p.labeled AS labeled, "
            "fc.cluster_id AS cid, fc.member_count AS count "
            "ORDER BY p.name"
        ).data()
    d.close()

    persons = {}
    for r in rows:
        name = r["name"]
        if name not in persons:
            persons[name] = {"name": name, "labeled": r["labeled"], "clusters": [], "total_faces": 0}
        persons[name]["clusters"].append({"cid": r["cid"], "count": r["count"]})
        persons[name]["total_faces"] += r["count"]
    return list(persons.values())


def get_photos_for_person(name):
    """Get all photos containing a person."""
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with d.session() as s:
        rows = s.run(
            "MATCH (p:Person {name: $name})<-[:IDENTIFIES]-(fc:FaceCluster)<-[:DEPICTS]-(m:Media) "
            "RETURN DISTINCT m.path AS path, m.media_id AS mid, m.timestamp AS ts "
            "ORDER BY m.timestamp DESC",
            name=name,
        ).data()
    d.close()
    return rows


def extract_face_thumbnail(path, bbox, size=128):
    """Extract a face thumbnail from the original image."""
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1
        pad = max(w, h) * 0.2
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(img.width, x2 + pad), min(img.height, y2 + pad)
        face = img.crop((x1, y1, x2, y2))
        face = face.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        face.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Photo Search</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f0f0f; color: #e0e0e0; padding: 20px; }
  h1 { color: #fff; margin-bottom: 8px; font-size: 24px; }
  .subtitle { color: #888; margin-bottom: 24px; font-size: 14px; }
  .search-bar { display: flex; gap: 12px; margin-bottom: 24px; }
  .search-input { flex: 1; padding: 14px 18px; border-radius: 12px; border: 1px solid #444;
                  background: #1a1a1a; color: #fff; font-size: 18px; }
  .search-input:focus { outline: none; border-color: #4fc3f7; }
  .btn { padding: 14px 24px; border-radius: 12px; border: none; cursor: pointer;
         font-size: 16px; font-weight: 600; }
  .btn-primary { background: #4fc3f7; color: #000; }
  .btn-primary:hover { background: #81d4fa; }
  .stats { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat { background: #1a1a1a; padding: 12px 16px; border-radius: 8px; min-width: 100px; }
  .stat-num { font-size: 20px; font-weight: bold; color: #4fc3f7; }
  .stat-label { font-size: 11px; color: #888; }
  .person-card { background: #1a1a1a; border-radius: 12px; padding: 16px;
                 margin-bottom: 12px; border: 1px solid #333; cursor: pointer; }
  .person-card:hover { border-color: #4fc3f7; }
  .person-card.selected { border-color: #4fc3f7; background: #1a2a2a; }
  .person-name { font-size: 18px; font-weight: 600; color: #fff; }
  .person-meta { font-size: 13px; color: #888; margin-top: 4px; }
  .person-faces { display: flex; gap: 6px; margin-top: 8px; }
  .face-thumb { width: 64px; height: 64px; border-radius: 6px; overflow: hidden; }
  .face-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .photos-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                 gap: 8px; margin-top: 20px; }
  .photo-card { background: #1a1a1a; border-radius: 8px; overflow: hidden; border: 1px solid #333; }
  .photo-card img { width: 100%; height: 150px; object-fit: cover; }
  .photo-card .meta { padding: 8px; font-size: 11px; color: #888; }
  .unlabeled { color: #555; font-style: italic; }
  .empty { color: #555; padding: 40px; text-align: center; }
  #results { min-height: 200px; }
</style>
</head>
<body>
<h1>Photo Search</h1>
<p class="subtitle">Find photos by person name</p>

<div class="search-bar">
  <input class="search-input" id="search" placeholder="Search for a person..."
         oninput="filterPersons()" autofocus>
</div>

<div class="stats" id="stats"></div>
<div id="results"></div>

<script>
let allPersons = [];
let selectedPerson = null;

async function init() {
  const resp = await fetch('/api/persons');
  allPersons = await resp.json();
  allPersons.sort((a, b) => b.total_faces - a.total_faces);
  renderStats();
  renderList(allPersons);
}

function renderStats() {
  const labeled = allPersons.filter(p => p.labeled).length;
  const total = allPersons.length;
  const faces = allPersons.reduce((s, p) => s + p.total_faces, 0);
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-num">${total}</div><div class="stat-label">Persons</div></div>
    <div class="stat"><div class="stat-num">${labeled}</div><div class="stat-label">Labeled</div></div>
    <div class="stat"><div class="stat-num">${total - labeled}</div><div class="stat-label">Unlabeled</div></div>
    <div class="stat"><div class="stat-num">${faces}</div><div class="stat-label">Total Faces</div></div>
  `;
}

function filterPersons() {
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = q ? allPersons.filter(p =>
    p.name.toLowerCase().includes(q)
  ) : allPersons;
  renderList(filtered);
}

function renderList(persons) {
  const el = document.getElementById('results');
  if (!persons.length) {
    el.innerHTML = '<div class="empty">No persons found</div>';
    return;
  }
  el.innerHTML = persons.map(p => `
    <div class="person-card ${selectedPerson === p.name ? 'selected' : ''}"
         onclick="selectPerson('${p.name.replace(/'/g, "\\'")}')">
      <div class="person-name ${p.labeled ? '' : 'unlabeled'}">${p.name}</div>
      <div class="person-meta">${p.total_faces} faces across ${p.clusters.length} clusters</div>
    </div>
  `).join('');
}

async function selectPerson(name) {
  selectedPerson = name;
  renderList(allPersons.filter(p => {
    const q = document.getElementById('search').value.toLowerCase();
    return !q || p.name.toLowerCase().includes(q);
  }));

  const el = document.getElementById('results');
  el.innerHTML += '<div style="color:#888;padding:20px;">Loading photos...</div>';

  const resp = await fetch(`/api/photos?name=${encodeURIComponent(name)}`);
  const photos = await resp.json();

  const photosHtml = photos.length ? `
    <div style="margin-top:16px;padding:12px;background:#1a2a2a;border-radius:8px;">
      <div style="font-size:16px;font-weight:600;margin-bottom:12px;">
        Photos of ${name} (${photos.length})
      </div>
      <div class="photos-grid">
        ${photos.map(p => `
          <div class="photo-card">
            <img src="file://${PHOTOS_ROOT.replace(/\\/g, '\\\\')}/${p.path.replace(PHOTOS_ROOT + '/', '')}"
                 alt="photo" loading="lazy" onerror="this.style.display='none'">
            <div class="meta">${p.ts || 'No date'}</div>
          </div>
        `).join('')}
      </div>
    </div>
  ` : '<div style="margin-top:12px;color:#888;">No photos found for this person.</div>';

  el.innerHTML += photosHtml;
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
        elif parsed.path == "/api/persons":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            persons = get_all_persons()
            self.wfile.write(json.dumps(persons).encode())
        elif parsed.path == "/api/photos":
            params = parse_qs(parsed.query)
            name = params.get("name", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            photos = get_photos_for_person(name)
            self.wfile.write(json.dumps(photos).encode())
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--photos-root", help="Override photos root path")
    args = ap.parse_args()

    global PHOTOS_ROOT, INGEST_DIR
    if args.photos_root:
        PHOTOS_ROOT = args.photos_root
        INGEST_DIR = os.path.join(PHOTOS_ROOT, "_ingest")

    print(f"[search-ui] serving at http://localhost:{args.port}")
    print(f"[search-ui] photos root: {PHOTOS_ROOT}")

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[search-ui] stopped")


if __name__ == "__main__":
    main()
