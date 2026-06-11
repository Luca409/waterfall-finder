#!/usr/bin/env python3
"""
Waterfall Finder Web Server
"""

import os, json, math, time, hashlib, tempfile, urllib.request, urllib.parse, urllib.error, ssl, sys, threading, secrets
from pathlib import Path
import numpy as np
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.windows import from_bounds
from shapely.geometry import shape, Point, MultiLineString, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address()


limiter = Limiter(
    app=app,
    key_func=client_ip,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)
MAX_CONCURRENT_JOBS_PER_IP = 2


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({"error": "Too many requests. Please slow down and try again."}), 429
os.makedirs("data/cache", exist_ok=True)
JOBS_DIR = Path("data/cache/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)
_jobs_lock = threading.Lock()
os.environ["GDAL_HTTP_UNSAFESSL"] = "YES"

# macOS ships without root certs for Python; bypass verification for USGS/Census APIs
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def urlopen(url_or_req, **kwargs):
    return urllib.request.urlopen(url_or_req, context=_ssl_ctx, **kwargs)
sys.setrecursionlimit(200000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def radius_bbox(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def utm_crs(lat, lon):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return epsg


def dem_tile_urls(W, S, E, N):
    urls = []
    for ilat in range(math.floor(S), math.ceil(N)):
        for ilon in range(math.floor(W), math.ceil(E)):
            n = ilat + 1
            w = -ilon
            urls.append(
                f"https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
                f"current/n{n:02d}w{w:03d}/USGS_13_n{n:02d}w{w:03d}.tif"
            )
    return urls


def fetch_dem(W, S, E, N, cache_key, on_progress=None):
    dem_path = f"data/cache/dem_{cache_key}.tif"
    if os.path.exists(dem_path):
        return dem_path

    margin = 0.02
    tile_urls = dem_tile_urls(W, S, E, N)
    tmp_files = []

    for i, url in enumerate(tile_urls):
        if on_progress:
            pct = 32 + int((i / max(len(tile_urls), 1)) * 18)
            on_progress(pct, f"Downloading elevation data ({i + 1}/{len(tile_urls)})…")
        try:
            print(f"  Opening DEM tile: {url.split('/')[-1]}")
            with rasterio.open(url) as src:
                win = from_bounds(W - margin, S - margin, E + margin, N + margin, src.transform)
                data = src.read(1, window=win)
                tf = src.window_transform(win)
                profile = src.profile.copy()
                profile.update(height=data.shape[0], width=data.shape[1],
                               transform=tf, compress="deflate")
                tmp = tempfile.NamedTemporaryFile(dir="data/cache", suffix=".tif", delete=False)
                tmp_files.append(tmp.name)
                tmp.close()
                with rasterio.open(tmp.name, "w", **profile) as dst:
                    dst.write(data, 1)
        except Exception as e:
            print(f"  Warning: skipping tile: {e}")

    if not tmp_files:
        raise RuntimeError("No DEM tiles found for this area (only US 3DEP coverage supported)")

    if len(tmp_files) == 1:
        os.rename(tmp_files[0], dem_path)
    else:
        srcs = [rasterio.open(f) for f in tmp_files]
        mosaic, out_tf = rio_merge(srcs)
        profile = srcs[0].profile.copy()
        profile.update(height=mosaic.shape[1], width=mosaic.shape[2],
                       transform=out_tf, compress="deflate")
        with rasterio.open(dem_path, "w", **profile) as dst:
            dst.write(mosaic)
        for s in srcs:
            s.close()
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    return dem_path


def fetch_flowlines(W, S, E, N, cache_key, on_progress=None):
    flow_path = f"data/cache/flowlines_{cache_key}.json"
    if os.path.exists(flow_path):
        if on_progress:
            on_progress(28, "Stream data loaded from cache")
        return json.load(open(flow_path))

    bbox_str = f"{W:.4f},{S:.4f},{E:.4f},{N:.4f}"
    base = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
    all_feats = []
    offset = 0
    while True:
        params = {
            "geometry": bbox_str, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "gnis_name,ftype,fcode,lengthkm",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson",
            "resultOffset": offset, "resultRecordCount": 1000
        }
        req = urllib.request.Request(
            base + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "research"}
        )
        data = json.load(urlopen(req, timeout=120))
        feats = data.get("features", [])
        all_feats.extend(feats)
        print(f"  Flowlines: offset {offset} +{len(feats)} (total {len(all_feats)})")
        if on_progress:
            on_progress(min(8 + offset // 500, 26), f"Fetching stream network ({len(all_feats)} segments)…")
        if len(feats) < 1000:
            break
        offset += 1000
        time.sleep(0.3)

    fc = {"type": "FeatureCollection", "features": all_feats}
    json.dump(fc, open(flow_path, "w"))
    return fc


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def run_analysis(lat, lon, radius_km, on_progress=None):
    def report(pct, label):
        if on_progress:
            on_progress(pct, label)

    W, S, E, N = radius_bbox(lat, lon, radius_km)
    bbox_sig = f"{W:.3f}{S:.3f}{E:.3f}{N:.3f}"
    cache_key = hashlib.md5(bbox_sig.encode()).hexdigest()[:10]

    results_path = f"data/cache/results_{cache_key}.json"
    if os.path.exists(results_path):
        print(f"Returning cached results for {cache_key}")
        report(100, "Using cached results")
        return json.load(open(results_path))

    utm_epsg = utm_crs(lat, lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True).transform
    to_ll  = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True).transform

    report(5, "Fetching stream network…")
    print(f"Fetching NHD flowlines...")
    flowlines_fc = fetch_flowlines(W, S, E, N, cache_key, on_progress=on_progress)

    report(30, "Fetching elevation data…")
    print(f"Fetching DEM...")
    dem_path = fetch_dem(W, S, E, N, cache_key, on_progress=on_progress)

    src_dem = rasterio.open(dem_path)
    dem_data = src_dem.read(1).astype(float)
    inv_tf = ~src_dem.transform

    def sample_elev(lons, lats):
        cols, rows = inv_tf * (np.asarray(lons), np.asarray(lats))
        rows = np.clip(rows.astype(int), 0, dem_data.shape[0] - 1)
        cols = np.clip(cols.astype(int), 0, dem_data.shape[1] - 1)
        return dem_data[rows, cols]

    report(52, "Building stream network…")
    print("Building stream network...")
    segs = []
    for f in flowlines_fc["features"]:
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        for g1 in (g.geoms if isinstance(g, MultiLineString) else [g]):
            try:
                gu = shp_transform(to_utm, g1)
            except Exception:
                continue
            if gu.length < 20:
                continue
            coords = list(gu.coords)
            (x0, y0) = coords[0]
            (x1, y1) = coords[-1]
            lon0, lat0 = to_ll(x0, y0)
            lon1, lat1 = to_ll(x1, y1)
            z0 = float(sample_elev([lon0], [lat0])[0])
            z1 = float(sample_elev([lon1], [lat1])[0])
            def grid_key(p):
                return (round(p[0] / 20), round(p[1] / 20))
            up, dn = ((x0, y0), (x1, y1)) if z0 >= z1 else ((x1, y1), (x0, y0))
            segs.append({
                "geom": gu, "len": gu.length,
                "up": grid_key(up), "dn": grid_key(dn),
                "name": (f["properties"].get("gnis_name") or "").strip(),
                "fcode": f["properties"].get("fcode"),
            })

    print(f"  {len(segs)} segments")
    report(62, f"Mapped {len(segs)} stream segments")

    into = {}
    for si, s in enumerate(segs):
        into.setdefault(s["dn"], []).append(si)

    memo = {}
    def upstream_len(si):
        if si in memo:
            return memo[si]
        memo[si] = segs[si]["len"] + sum(
            upstream_len(j) for j in into.get(segs[si]["up"], []) if j != si
        )
        return memo[si]
    for si in range(len(segs)):
        upstream_len(si)

    # Scan elevation profiles for steep drops
    STEP_M   = 10.0
    WINDOW_M = 50
    MIN_DROP = 4.0
    k = int(WINDOW_M / STEP_M)

    report(68, "Scanning elevation profiles…")
    print("Scanning elevation profiles...")
    hits = []
    n_segs = len(segs)
    for si, s in enumerate(segs):
        if n_segs and si % max(1, n_segs // 25) == 0:
            report(68 + int((si / n_segs) * 22), f"Scanning streams ({si}/{n_segs})…")
        gu = s["geom"]
        n = max(int(gu.length / STEP_M) + 1, 7)
        dists = np.linspace(0, gu.length, n)
        pts = [gu.interpolate(d) for d in dists]
        xs = np.array([p.x for p in pts])
        ys = np.array([p.y for p in pts])
        lons_arr, lats_arr = to_ll(xs, ys)
        z = sample_elev(lons_arr, lats_arr)
        if z[-1] > z[0]:
            z = z[::-1]; lons_arr = lons_arr[::-1]; lats_arr = lats_arr[::-1]
        zm = np.minimum.accumulate(z)
        if len(zm) <= k:
            continue
        drops = zm[:-k] - zm[k:]
        for j in np.where(drops >= MIN_DROP)[0]:
            mid = j + k // 2
            pt_lon = float(lons_arr[mid])
            pt_lat = float(lats_arr[mid])
            # Filter to radius
            dist_km = math.sqrt((pt_lat - lat)**2 + (pt_lon - lon)**2) * 111.0
            if dist_km > radius_km:
                continue
            hits.append({
                "si": si, "lat": pt_lat, "lon": pt_lon,
                "drop50": float(drops[j]), "elev": float(zm[mid])
            })

    print(f"  {len(hits)} raw hits")
    report(92, "Clustering waterfall candidates…")

    # Cluster within 120m
    hits.sort(key=lambda h: -h["drop50"])
    clusters = []
    for h in hits:
        p = np.array(to_utm(h["lon"], h["lat"]))
        if not any(np.hypot(*(p - c["_p"])) < 120 for c in clusters):
            s = segs[h["si"]]
            clusters.append({**h, "_p": p, "stream": s["name"],
                             "uplen_km": memo[h["si"]] / 1000.0})

    # Merge gorge clusters within 400m, apply min thresholds
    sites = []
    for c in sorted(clusters, key=lambda x: -x["drop50"]):
        if c["drop50"] < 6:
            continue
        p = np.array(to_utm(c["lon"], c["lat"]))
        if any(np.hypot(*(p - s["_p"])) < 400 for s in sites):
            continue
        if not c["stream"]:
            continue
        drop = round(c["drop50"], 1)
        if drop >= 25:
            size = "big"
        elif drop >= 12:
            size = "medium"
        else:
            size = "small"
        sites.append({
            "lat": c["lat"], "lon": c["lon"],
            "stream": c["stream"],
            "drop_m": drop,
            "elevation_m": round(c["elev"]),
            "size": size,
            "_p": p,
        })

    src_dem.close()
    print(f"  {len(sites)} waterfall candidates")
    report(98, f"Found {len(sites)} candidates")

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            "properties": {
                "stream": s["stream"],
                "drop_m": s["drop_m"],
                "elevation_m": s["elevation_m"],
                "size": s["size"],
            },
        }
        for s in sorted(sites, key=lambda x: -x["drop_m"])
    ]
    result = {"type": "FeatureCollection", "features": features}
    json.dump(result, open(results_path, "w"))
    print(f"  Results cached → {results_path}")
    report(100, "Done")
    return result


# ---------------------------------------------------------------------------
# Background search jobs (file-backed so multiple gunicorn workers can share state)
# ---------------------------------------------------------------------------

def _job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def active_jobs_for_ip(ip):
    count = 0
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if job.get("client_ip") == ip and job.get("status") in ("pending", "running"):
            count += 1
    return count


def create_job(lat, lon, radius_km, client_ip=None):
    job_id = secrets.token_hex(8)
    job = {
        "id": job_id,
        "status": "pending",
        "pct": 0,
        "label": "Queued…",
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "client_ip": client_ip,
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _job_path(job_id).write_text(json.dumps(job))
    return job_id


def get_job(job_id):
    path = _job_path(job_id)
    if not path.exists():
        return None
    with _jobs_lock:
        return json.loads(path.read_text())


def update_job(job_id, **fields):
    with _jobs_lock:
        job = json.loads(_job_path(job_id).read_text())
        job.update(fields)
        _job_path(job_id).write_text(json.dumps(job))


def _run_job(job_id, lat, lon, radius_km):
    try:
        update_job(job_id, status="running", pct=2, label="Starting search…")

        def on_progress(pct, label):
            update_job(job_id, pct=pct, label=label)

        result = run_analysis(lat, lon, radius_km, on_progress=on_progress)
        update_job(job_id, status="done", pct=100, label="Done", result=result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_job(job_id, status="error", error=str(e))


def start_job(job_id, lat, lon, radius_km):
    threading.Thread(target=_run_job, args=(job_id, lat, lon, radius_km), daemon=True).start()


def wait_for_job(job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get_job(job_id)
        if not job:
            return None
        if job["status"] == "done":
            return job
        if job["status"] == "error":
            return job
        time.sleep(0.2)
    return get_job(job_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Waterfall Finder</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet-geosearch@3.11.1/dist/geosearch.css"/>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; height: 100vh; display: flex; flex-direction: column; }
    #toolbar {
      background: #1a1a2e; color: #eee; padding: 10px 16px;
      display: flex; align-items: center; gap: 16px; flex-shrink: 0;
      box-shadow: 0 2px 6px rgba(0,0,0,0.4);
    }
    #toolbar h1 { font-size: 1.1rem; font-weight: 600; white-space: nowrap; margin-right: 8px; }
    #toolbar label { font-size: 0.85rem; color: #aaa; }
    #toolbar input[type=number] {
      width: 70px; padding: 4px 6px; border-radius: 4px;
      border: 1px solid #444; background: #2a2a3e; color: #eee; font-size: 0.9rem;
    }
    #coords-display { font-size: 0.8rem; color: #8888cc; white-space: nowrap; }
    #search-btn {
      padding: 7px 18px; background: #4f8ef7; color: #fff; border: none;
      border-radius: 5px; cursor: pointer; font-size: 0.9rem; font-weight: 600;
      white-space: nowrap;
    }
    #search-btn:hover { background: #3a7ae0; }
    #search-btn:disabled { background: #555; cursor: not-allowed; }
    #status { font-size: 0.82rem; color: #aaa; white-space: nowrap; }
    #progress-track {
      height: 0; overflow: hidden; background: #12121f; flex-shrink: 0;
      transition: height 0.2s ease;
    }
    #progress-track.active { height: 4px; }
    #progress-bar {
      height: 100%; width: 0; background: linear-gradient(90deg, #4f8ef7, #6ee7b7);
      transition: width 0.35s ease;
    }
    #map { flex: 1; }
    .wf-popup { min-width: 180px; }
    .wf-popup b { font-size: 1rem; }
    .wf-popup .stat { color: #555; font-size: 0.85rem; margin-top: 2px; }
    .hint { font-size: 0.78rem; color: #aaa; white-space: nowrap; }
    #welcome-modal {
      display: none; position: fixed; inset: 0; z-index: 2000;
      background: rgba(0, 0, 0, 0.55); align-items: center; justify-content: center;
    }
    #welcome-modal.open { display: flex; }
    #welcome-modal .dialog {
      background: #fff; color: #222; max-width: 420px; margin: 16px;
      padding: 24px 28px; border-radius: 10px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
    }
    #welcome-modal h2 { font-size: 1.15rem; margin-bottom: 10px; }
    #welcome-modal p { font-size: 0.95rem; line-height: 1.5; color: #444; }
    #welcome-modal button {
      margin-top: 18px; padding: 8px 20px; background: #4f8ef7; color: #fff;
      border: none; border-radius: 5px; font-size: 0.9rem; font-weight: 600; cursor: pointer;
    }
    #welcome-modal button:hover { background: #3a7ae0; }
  </style>
</head>
<body>
<div id="welcome-modal" class="open">
  <div class="dialog">
    <h2>Welcome</h2>
    <p>Click anywhere on the map and then search to find waterfalls.</p>
    <button type="button" onclick="closeWelcomeModal()">Got it</button>
  </div>
</div>
<div id="toolbar">
  <h1>💧 Waterfall Finder</h1>
  <span class="hint">Click map to set center</span>
  <span id="coords-display">No center set</span>
  <label>Radius (km)
    <input type="number" id="radius" value="30" min="1" max="100" step="1"/>
  </label>
  <button id="search-btn" onclick="doSearch()">Search</button>
  <span id="status"></span>
</div>
<div id="progress-track"><div id="progress-bar"></div></div>
<div id="map"></div>

<div style="position:absolute;bottom:30px;right:10px;z-index:1000;background:white;padding:10px 14px;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,0.25);font-size:0.82rem;">
  <div style="display:flex;align-items:center;gap:7px;margin:3px 0"><span style="width:12px;height:12px;border-radius:50%;background:#e74c3c;display:inline-block"></span> Likely big (&ge;25 m)</div>
  <div style="display:flex;align-items:center;gap:7px;margin:3px 0"><span style="width:12px;height:12px;border-radius:50%;background:#e67e22;display:inline-block"></span> Likely medium (12–25 m)</div>
  <div style="display:flex;align-items:center;gap:7px;margin:3px 0"><span style="width:12px;height:12px;border-radius:50%;background:#f1c40f;display:inline-block"></span> Likely small (6–12 m)</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DEFAULT_LAT = 42.42457;
const DEFAULT_LON = -74.40353;
const DEFAULT_RADIUS_KM = 30;

const map = L.map('map').setView([DEFAULT_LAT, DEFAULT_LON], 11);

L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxZoom: 17,
}).addTo(map);

let centerMarker = null;
let radiusCircle = null;
let resultsLayer = L.featureGroup().addTo(map);
let centerLatLon = null;

function dotIcon(color) {
  return L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.5)"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

const sizeColors = { small: '#f1c40f', medium: '#e67e22', big: '#e74c3c' };

function setSearchCenter(latlng) {
  centerLatLon = latlng;
  document.getElementById('coords-display').textContent =
    `${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`;

  if (centerMarker) map.removeLayer(centerMarker);
  centerMarker = L.marker(latlng, {
    icon: L.divIcon({
      className: '',
      html: '<div style="width:18px;height:18px;border-radius:50%;background:#fff;border:3px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.6)"></div>',
      iconSize: [18, 18], iconAnchor: [9, 9],
    })
  }).addTo(map).bindPopup('Search center');

  updateCircle();
}

function closeWelcomeModal() {
  document.getElementById('welcome-modal').classList.remove('open');
}

map.on('click', function(e) {
  setSearchCenter(e.latlng);
});

setSearchCenter(L.latLng(DEFAULT_LAT, DEFAULT_LON));

document.getElementById('radius').addEventListener('input', updateCircle);

function updateCircle() {
  if (!centerLatLon) return;
  const r = parseFloat(document.getElementById('radius').value) * 1000;
  if (radiusCircle) map.removeLayer(radiusCircle);
  radiusCircle = L.circle(centerLatLon, {
    radius: r, color: '#4f8ef7', weight: 2,
    fillColor: '#4f8ef7', fillOpacity: 0.08
  }).addTo(map);
}

function setProgress(pct, label) {
  const track = document.getElementById('progress-track');
  const bar = document.getElementById('progress-bar');
  const status = document.getElementById('status');
  track.classList.add('active');
  bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
  status.textContent = label;
}

function hideProgress() {
  const track = document.getElementById('progress-track');
  const bar = document.getElementById('progress-bar');
  track.classList.remove('active');
  bar.style.width = '0%';
}

function parseSseChunk(chunk) {
  const events = [];
  for (const block of chunk.split('\\n\\n')) {
    if (!block.trim()) continue;
    let event = 'message', data = '';
    for (const line of block.split('\\n')) {
      if (line.startsWith('event: ')) event = line.slice(7);
      else if (line.startsWith('data: ')) data = line.slice(6);
    }
    if (data) events.push({ event, data: JSON.parse(data) });
  }
  return events;
}

async function doSearch() {
  if (!centerLatLon) { alert('Click the map to set a search center first.'); return; }
  const radius = parseFloat(document.getElementById('radius').value);
  const btn = document.getElementById('search-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  setProgress(2, 'Starting search…');
  resultsLayer.clearLayers();

  try {
    const startRes = await fetch('/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat: centerLatLon.lat, lon: centerLatLon.lng, radius_km: radius }),
    });
    if (!startRes.ok) {
      const err = await startRes.json().catch(() => ({ error: startRes.statusText }));
      throw new Error(err.error || 'Search failed');
    }
    const { job_id } = await startRes.json();
    if (!job_id) throw new Error('No job id returned');

    const res = await fetch(`/search/${job_id}/events`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || 'Search failed');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fc = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\\n\\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        for (const evt of parseSseChunk(part + '\\n\\n')) {
          if (evt.event === 'progress') {
            setProgress(evt.data.pct, evt.data.label);
          } else if (evt.event === 'error') {
            throw new Error(evt.data.message);
          } else if (evt.event === 'result') {
            fc = evt.data;
          }
        }
      }
    }
    if (buffer.trim()) {
      for (const evt of parseSseChunk(buffer)) {
        if (evt.event === 'progress') setProgress(evt.data.pct, evt.data.label);
        else if (evt.event === 'error') throw new Error(evt.data.message);
        else if (evt.event === 'result') fc = evt.data;
      }
    }
    if (!fc) throw new Error('No results returned');

    fc.features.forEach(f => {
      const p = f.properties;
      const color = sizeColors[p.size] || '#888';
      const marker = L.marker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {
        icon: dotIcon(color),
        zIndexOffset: p.size === 'big' ? 200 : p.size === 'medium' ? 100 : 0,
      });
      marker.bindPopup(`
        <div class="wf-popup">
          <b>${p.stream}</b>
          <div class="stat">Drop: <b>${p.drop_m} m</b> over 50 m window</div>
          <div class="stat">Elevation: ${p.elevation_m} m</div>
          <div class="stat">Size: likely ${p.size} waterfall</div>
          <div style="margin-top:6px">
            <a href="https://www.google.com/maps?q=${f.geometry.coordinates[1]},${f.geometry.coordinates[0]}" target="_blank">Open in Google Maps</a>
          </div>
        </div>
      `);
      resultsLayer.addLayer(marker);
    });

    const count = fc.features.length;
    status.textContent = `Found ${count} candidate${count !== 1 ? 's' : ''}.`;
    if (count > 0) {
      const latlngs = fc.features.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
      map.fitBounds(L.latLngBounds(latlngs).pad(0.2));
    }
  } catch(e) {
    status.textContent = 'Request failed: ' + e.message;
    hideProgress();
  } finally {
    btn.disabled = false;
    setTimeout(hideProgress, 600);
  }
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return HTML


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_search_params(data):
    lat = float(data["lat"])
    lon = float(data["lon"])
    radius_km = float(data["radius_km"])
    if radius_km > 150:
        raise ValueError("Radius too large (max 150 km)")
    return lat, lon, radius_km


@app.route("/search", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour")
def search():
    data = request.get_json()
    try:
        lat, lon, radius_km = _parse_search_params(data)
    except (TypeError, ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400

    ip = client_ip()
    if active_jobs_for_ip(ip) >= MAX_CONCURRENT_JOBS_PER_IP:
        return jsonify({"error": "Too many searches in progress. Please wait."}), 429

    job_id = create_job(lat, lon, radius_km, client_ip=ip)
    start_job(job_id, lat, lon, radius_km)

    if request.headers.get("Accept") == "application/json" and request.args.get("wait") == "1":
        job = wait_for_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] == "error":
            return jsonify({"error": job["error"]}), 500
        if job["status"] != "done":
            return jsonify({"error": "Search timed out"}), 504
        return jsonify(job["result"])

    return jsonify({"job_id": job_id}), 202


@app.route("/search/<job_id>/events")
@limiter.limit("30 per minute")
def search_events(job_id):
    if not get_job(job_id):
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def generate():
        last_pct = -1
        last_label = ""
        while True:
            job = get_job(job_id)
            if not job:
                yield _sse("error", {"message": "Job not found"})
                break
            if job["pct"] != last_pct or job["label"] != last_label:
                last_pct = job["pct"]
                last_label = job["label"]
                yield _sse("progress", {"pct": job["pct"], "label": job["label"]})
            if job["status"] == "done":
                yield _sse("result", job["result"])
                break
            if job["status"] == "error":
                yield _sse("error", {"message": job["error"]})
                break
            time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    print("Starting Waterfall Finder at http://localhost:8080")
    app.run(debug=False, port=8080)
