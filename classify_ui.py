#!/usr/bin/env python3
"""
classify_ui.py — Fast face cluster labeling tool.

Lazy loads thumbnails, keyboard shortcuts, filter by labeled/unlabeled.

Usage:
  python classify_ui.py [--port 8080] [--photos-root /path/to/photos]
"""
import os, sys, json, argparse, base64, io
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_config as config

PHOTOS_ROOT = config.PHOTOS_ROOT_HOST
INGEST_DIR = config.INGEST_DIR_HOST
EMB_FILE = os.path.join(INGEST_DIR, "face_embeddings.jsonl")
INV_FILE = os.path.join(INGEST_DIR, "media_inventory.jsonl")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://100.64.43.123:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "knowledge_graph_2026")

_media_cache = None
_cluster_cache = None
_thumb_cache = {}
_thumb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".thumb_cache")


def get_media():
    global _media_cache
    if _media_cache is None:
        _media_cache = {}
        if os.path.exists(INV_FILE):
            with open(INV_FILE) as f:
                for line in f:
                    r = json.loads(line.strip())
                    _media_cache[r["media_id"]] = r
    return _media_cache


def get_clusters():
    global _cluster_cache
    if _cluster_cache is not None:
        return _cluster_cache

    raw = {}
    with open(EMB_FILE) as f:
        for line in f:
            r = json.loads(line.strip())
            cid = r["cluster_id"]
            if cid not in raw:
                raw[cid] = []
            raw[cid].append(r)

    _cluster_cache = {}
    for cid, faces in raw.items():
        _cluster_cache[cid] = {
            "id": cid,
            "count": len(faces),
            "faces": faces,
        }
    return _cluster_cache


def get_persons():
    try:
        from neo4j import GraphDatabase
        d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        with d.session() as s:
            rows = s.run(
                "MATCH (fc:FaceCluster)-[:IDENTIFIES]->(p:Person) "
                "RETURN fc.cluster_id AS cid, p.name AS name, p.labeled AS labeled"
            ).data()
        d.close()
        return {r["cid"]: {"name": r["name"], "labeled": r["labeled"]} for r in rows}
    except Exception:
        return {}


def label_cluster(cid, name):
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with d.session() as s:
        s.run(
            "MATCH (fc:FaceCluster {cluster_id: $cid})-[:IDENTIFIES]->(p:Person) "
            "SET p.name = $name, p.labeled = true",
            cid=cid, name=name,
        )
    d.close()


def _cache_key(path, bbox, size):
    import hashlib
    h = hashlib.md5(f"{path}:{bbox}:{size}".encode()).hexdigest()
    return h


def thumb(path, bbox, size=96):
    key = _cache_key(path, bbox, size)
    if key in _thumb_cache:
        return _thumb_cache[key]
    cache_path = os.path.join(_thumb_dir, f"{key}.jpg")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        _thumb_cache[key] = data
        return data
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        x1, y1, x2, y2 = bbox
        pad = max(x2 - x1, y2 - y1) * 0.3
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(img.width, x2 + pad), min(img.height, y2 + pad)
        face = img.crop((x1, y1, x2, y2)).resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        face.save(buf, format="JPEG", quality=75)
        data = base64.b64encode(buf.getvalue()).decode()
        os.makedirs(_thumb_dir, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(base64.b64decode(data))
        _thumb_cache[key] = data
        return data
    except Exception:
        return None


def photo_thumb(path, max_sz=300):
    key = _cache_key(path, [], max_sz)
    if key in _thumb_cache:
        return _thumb_cache[key]
    cache_path = os.path.join(_thumb_dir, f"{key}.jpg")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        _thumb_cache[key] = data
        return data
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        img.thumbnail((max_sz, max_sz), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        data = base64.b64encode(buf.getvalue()).decode()
        os.makedirs(_thumb_dir, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(base64.b64decode(data))
        _thumb_cache[key] = data
        return data
    except Exception:
        return None


def photo_with_box(path, bbox, max_sz=500):
    try:
        from PIL import Image, ImageDraw
        img = Image.open(path).convert("RGB")
        if max(img.width, img.height) > max_sz:
            r = max_sz / max(img.width, img.height)
            img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
            bbox = [int(b * r) for b in bbox]
        ImageDraw.Draw(img).rectangle(bbox, outline="#4fc3f7", width=3)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Face Labeler</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0a;color:#e0e0e0;overflow:hidden;height:100vh}
.app{display:grid;grid-template-columns:280px 1fr;grid-template-rows:56px 1fr;height:100vh}
.topbar{grid-column:1/-1;background:#111;border-bottom:1px solid #222;display:flex;align-items:center;padding:0 20px;gap:16px}
.topbar h1{font-size:16px;color:#4fc3f7;white-space:nowrap}
.topbar .stats{font-size:12px;color:#888}
.sidebar{background:#111;border-right:1px solid #222;overflow-y:auto;display:flex;flex-direction:column}
.sidebar-header{padding:12px;border-bottom:1px solid #222;position:sticky;top:0;background:#111;z-index:2}
.sidebar-header input{width:100%;padding:8px 12px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:13px}
.sidebar-header input:focus{outline:none;border-color:#4fc3f7}
.filter-row{display:flex;gap:4px;margin-top:8px}
.filter-btn{flex:1;padding:4px;border-radius:4px;border:1px solid #333;background:#1a1a1a;color:#888;font-size:11px;cursor:pointer;text-align:center}
.filter-btn.active{border-color:#4fc3f7;color:#4fc3f7;background:#0a1a2a}
.cluster-list{flex:1;overflow-y:auto;padding:4px}
.ci{display:flex;align-items:center;gap:8px;padding:8px;border-radius:6px;cursor:pointer;font-size:12px}
.ci:hover{background:#1a1a1a}
.ci.active{background:#1a2a2a;border:1px solid #4fc3f7}
.ci .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.ci .dot.labeled{background:#66bb6a}
.ci .dot.unlabeled{background:#444}
.ci .info{flex:1;min-width:0}
.ci .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci .ct{color:#888;font-size:11px}
.main{overflow-y:auto;padding:20px}
.empty{color:#555;text-align:center;padding:80px 20px;font-size:14px}
.detail{max-width:800px}
.label-row{display:flex;gap:8px;margin-bottom:16px;position:sticky;top:0;background:#0a0a0a;padding:8px 0;z-index:5}
.label-row input{flex:1;padding:10px 14px;border-radius:8px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:15px}
.label-row input:focus{outline:none;border-color:#4fc3f7}
.btn{padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:600}
.btn-p{background:#4fc3f7;color:#000}.btn-p:hover{background:#81d4fa}
.btn-g{background:#222;color:#aaa}.btn-g:hover{background:#333}
.btn-d{background:#c62828;color:#fff}.btn-d:hover{background:#e53935}
.cluster-info{font-size:13px;color:#888;margin-bottom:12px}
.faces-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-bottom:20px}
.face-card{background:#111;border-radius:6px;overflow:hidden;border:1px solid #222}
.face-card img{width:100%;height:150px;object-fit:cover}
.face-card .meta{padding:6px 8px;font-size:10px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.photos-section{margin-top:20px}
.photos-section h3{font-size:13px;color:#888;margin-bottom:8px}
.photo-row{display:flex;gap:6px;flex-wrap:wrap}
.photo-card{background:#111;border-radius:6px;overflow:hidden;border:1px solid #222;flex:0 0 180px}
.photo-card img{width:180px;height:135px;object-fit:cover}
.photo-card .meta{padding:4px 6px;font-size:9px;color:#666}
.toast{position:fixed;bottom:20px;right:20px;background:#1a2a1a;border:1px solid #66bb6a;padding:10px 16px;border-radius:8px;font-size:13px;z-index:100;display:none}
.help{position:fixed;bottom:20px;left:300px;font-size:11px;color:#555}
.help kbd{background:#222;padding:2px 6px;border-radius:3px;border:1px solid #333;font-size:10px}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <h1>Face Labeler</h1>
    <div class="stats" id="stats"></div>
  </div>
  <div class="sidebar">
    <div class="sidebar-header">
      <input id="search" placeholder="Search clusters..." oninput="applyFilter()">
      <div class="filter-row">
        <div class="filter-btn active" data-f="all" onclick="setFilter('all',this)">All</div>
        <div class="filter-btn" data-f="unlabeled" onclick="setFilter('unlabeled',this)">Unlabeled</div>
        <div class="filter-btn" data-f="labeled" onclick="setFilter('labeled',this)">Labeled</div>
      </div>
    </div>
    <div class="cluster-list" id="clist"></div>
  </div>
  <div class="main" id="main">
    <div class="empty">Select a cluster or press <kbd>Space</kbd> to start</div>
  </div>
</div>
<div class="help">
  <kbd>Space</kbd> Start &nbsp; <kbd>j</kbd>/<kbd>k</kbd> Nav &nbsp; <kbd>Enter</kbd> Label &nbsp; <kbd>s</kbd> Skip &nbsp; <kbd>l</kbd> Focus input
</div>
<div class="toast" id="toast"></div>

<script>
let C=[],P={},filt=[],cur=null,filter='all',idx=0;
const $=s=>document.getElementById(s);

async function init(){
  const t0=performance.now();
  const [cResp,pResp]=await Promise.all([fetch('/api/clusters'),fetch('/api/persons')]);
  C=await cResp.json();
  const pList=await pResp.json();
  P={};pList.forEach(p=>P[p.cid]=p);
  C.sort((a,b)=>b.count-a.count);
  applyFilter();
  const ms=Math.round(performance.now()-t0);
  $('stats').textContent=`${C.length} clusters, ${pList.filter(p=>p.labeled).length} labeled, loaded in ${ms}ms`;
}

function applyFilter(){
  const q=$('search').value.toLowerCase();
  filt=C.filter(c=>{
    if(filter==='labeled'&&!P[c.id]?.labeled)return false;
    if(filter==='unlabeled'&&P[c.id]?.labeled)return false;
    if(q){
      const nm=(P[c.id]?.name||'').toLowerCase();
      return nm.includes(q)||String(c.id).includes(q);
    }
    return true;
  });
  renderList();
}

function setFilter(f,el){
  filter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  applyFilter();
}

function renderList(){
  const el=$('clist');
  const vis=filt.slice(0,200);
  el.innerHTML=vis.map((c,i)=>{
    const p=P[c.id]||{};
    const nm=p.name||'Unknown';
    const labeled=!!p.labeled;
    return`<div class="ci ${cur===c.id?'active':''}" onclick="go(${c.id})" data-idx="${i}">
      <div class="dot ${labeled?'labeled':'unlabeled'}"></div>
      <div class="info"><div class="nm">${nm}</div><div class="ct">${c.count} faces</div></div>
    </div>`;
  }).join('');
  if(filt.length>200){
    el.innerHTML+=`<div style="padding:12px;color:#555;text-align:center;font-size:12px">Showing 200 of ${filt.length}</div>`;
  }
}

async function go(cid){
  cur=cid;idx=filt.findIndex(c=>c.id===cid);
  renderList();
  const el=$('main');
  el.innerHTML='<div class="empty">Loading...</div>';
  const r=await fetch(`/api/detail/${cid}`);
  const d=await r.json();
  const p=P[cid]||{};
  const nm=p.name||'Unknown';

  el.innerHTML=`
    <div class="detail">
      <div class="label-row">
        <input id="ni" placeholder="Enter name..." value="${p.labeled?nm:''}"
               onkeydown="if(event.key==='Enter')doLabel(${cid})">
        <button class="btn btn-p" onclick="doLabel(${cid})">Label</button>
        <button class="btn btn-g" onclick="skip()">Skip</button>
      </div>
      <div class="cluster-info">
        Cluster ${cid} &middot; ${d.count} faces &middot; ${d.photos.length} photos
        ${p.labeled?' &middot; <span style="color:#66bb6a">Labeled: '+nm+'</span>':''}
      </div>
      ${d.photos.length?`
      <div class="photos-section">
        <h3>Photos containing this person (${d.photos.length})</h3>
        <div class="photo-row">${d.photos.slice(0,16).map(ph=>`
          <div class="photo-card">
            <img src="data:image/jpeg;base64,${ph.img}" loading="lazy">
            <div class="meta">${ph.ts||''}</div>
          </div>`).join('')}
        </div>
      </div>`:''}
      <div class="photos-section">
        <h3>Face samples (${d.faces.length})</h3>
        <div class="faces-grid">${d.faces.map(f=>`
          <div class="face-card">
            <img src="data:image/jpeg;base64,${f.img}" loading="lazy">
            <div class="meta">${f.fn||''}</div>
          </div>`).join('')}
        </div>
      </div>
    </div>`;
  $('ni').focus();
}

async function doLabel(cid){
  const nm=$('ni').value.trim();
  if(!nm)return;
  await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cluster_id:cid,name:nm})});
  P[cid]={name:nm,labeled:true,cid};
  toast('Labeled as '+nm);
  applyFilter();
  skip();
}

function skip(){
  if(idx<filt.length-1)go(filt[idx+1].id);
  else toast('Done!');
}

function toast(m){const e=$('toast');e.textContent=m;e.style.display='block';setTimeout(()=>e.style.display='none',1500)}

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key===' '||e.key==='Enter'){e.preventDefault();if(!cur&&filt.length)go(filt[0].id)}
  if(e.key==='j'||e.key==='ArrowDown'){e.preventDefault();skip()}
  if(e.key==='k'||e.key==='ArrowUp'){e.preventDefault();if(idx>0)go(filt[idx-1].id)}
  if(e.key==='s'){e.preventDefault();skip()}
  if(e.key==='l'){e.preventDefault();$('ni')?.focus()}
});

init();
</script>
</body>
</html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

        elif p.path == "/api/clusters":
            clusters = get_clusters()
            out = [{"id": cid, "count": c["count"]} for cid, c in clusters.items()]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        elif p.path == "/api/persons":
            persons = get_persons()
            out = [{"cid": k, "name": v["name"], "labeled": v["labeled"]} for k, v in persons.items()]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        elif p.path.startswith("/api/detail/"):
            cid = int(p.path.split("/")[-1])
            clusters = get_clusters()
            media = get_media()
            c = clusters.get(cid, {"count": 0, "faces": []})

            faces_out = []
            photo_ids = set()
            for f in c["faces"][:16]:
                mid = f["media_id"]
                m = media.get(mid, {})
                path = m.get("path", "")
                photo_ids.add(mid)
                img = None
                if path and os.path.exists(path):
                    img = thumb(path, f["bbox"])
                faces_out.append({"img": img, "fn": os.path.basename(path) if path else ""})

            photos_out = []
            for mid in list(photo_ids)[:10]:
                m = media.get(mid, {})
                path = m.get("path", "")
                img = None
                if path and os.path.exists(path):
                    img = photo_thumb(path, 300)
                photos_out.append({"img": img, "ts": m.get("timestamp", "")})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": c["count"], "faces": faces_out, "photos": photos_out}).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/label":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            label_cluster(body["cluster_id"], body["name"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--photos-root")
    args = ap.parse_args()

    global PHOTOS_ROOT, INGEST_DIR, EMB_FILE, INV_FILE
    if args.photos_root:
        PHOTOS_ROOT = args.photos_root
        INGEST_DIR = os.path.join(PHOTOS_ROOT, "_ingest")
        EMB_FILE = os.path.join(INGEST_DIR, "face_embeddings.jsonl")
        INV_FILE = os.path.join(INGEST_DIR, "media_inventory.jsonl")

    print(f"[classify-ui] http://localhost:{args.port}")
    HTTPServer(("0.0.0.0", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
