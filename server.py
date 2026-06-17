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

from analytics import record as record_event, summary as analytics_summary

app = Flask(__name__)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address()


def client_user_agent():
    return request.headers.get("User-Agent", "")


limiter = Limiter(
    app=app,
    key_func=client_ip,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)
MAX_CONCURRENT_JOBS_PER_IP = 1
DEFAULT_LAT = 42.42457
DEFAULT_LON = -74.40353
DEFAULT_RADIUS_KM = 15
ALGORITHM_VERSION = 3
VALID_SIZES = {"small", "medium", "big"}
REQUIRED_FEATURE_PROPS = {"stream", "drop_m", "elevation_m", "size"}
STREAM_FTYPE = 460
EXCLUDED_FTYPES = {336, 343, 558}
MIN_PROFILE_DROP_M = 12.0
SHORT_WINDOW_M = 20.0
MIN_SHARP_DROP_M = 8.0
MIN_SHARP_RATIO = 0.45
MIN_CONFIDENCE_SCORE = 5
CACHE_DEDUPE_M = 150.0


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


def fetch_official_water_features(W, S, E, N, cache_key):
    feature_path = f"data/cache/official_waterfalls_{cache_key}.json"
    if os.path.exists(feature_path):
        return json.load(open(feature_path))

    bbox_str = f"{W:.4f},{S:.4f},{E:.4f},{N:.4f}"
    features = []
    for layer_id in (487, 431):  # NHD Waterfall, Rapids
        base = f"https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/{layer_id}/query"
        params = {
            "geometry": bbox_str, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "gnis_name,ftype,fcode",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson",
        }
        req = urllib.request.Request(
            base + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "research"}
        )
        try:
            data = json.load(urlopen(req, timeout=60))
        except Exception as e:
            print(f"  Warning: skipping official water feature layer {layer_id}: {e}")
            continue
        features.extend(data.get("features", []))

    fc = {"type": "FeatureCollection", "features": features}
    json.dump(fc, open(feature_path, "w"))
    return fc


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def results_cache_key(lat, lon, radius_km):
    W, S, E, N = radius_bbox(lat, lon, radius_km)
    bbox_sig = f"{W:.3f}{S:.3f}{E:.3f}{N:.3f}"
    return hashlib.md5(bbox_sig.encode()).hexdigest()[:10]


def _feature_distance_m(a_lon, a_lat, b_lon, b_lat):
    mean_lat = math.radians((a_lat + b_lat) / 2.0)
    dx = (a_lon - b_lon) * 111_320.0 * math.cos(mean_lat)
    dy = (a_lat - b_lat) * 110_540.0
    return math.hypot(dx, dy)


def _is_valid_size(value):
    return value in VALID_SIZES


def _valid_cache_feature(feature):
    if feature.get("type") != "Feature":
        return False
    coords = feature.get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        return False
    props = feature.get("properties", {})
    if not REQUIRED_FEATURE_PROPS.issubset(props):
        return False
    if not _is_valid_size(props.get("size")):
        return False
    try:
        float(coords[0])
        float(coords[1])
        float(props["drop_m"])
        float(props["elevation_m"])
    except (TypeError, ValueError):
        return False
    return bool(str(props.get("stream", "")).strip())


def _sanitize_cached_results(fc, path=None):
    if fc.get("algorithm_version") != ALGORITHM_VERSION:
        if path:
            try:
                path.unlink()
            except OSError:
                pass
        return None

    valid_features = [f for f in fc.get("features", []) if _valid_cache_feature(f)]
    if len(valid_features) != len(fc.get("features", [])):
        if valid_features:
            fc = {**fc, "features": valid_features}
            if path:
                path.write_text(json.dumps(fc))
        elif path:
            try:
                path.unlink()
            except OSError:
                pass
            return None
    return {**fc, "features": valid_features}


def _cache_result(features):
    return {
        "type": "FeatureCollection",
        "algorithm_version": ALGORITHM_VERSION,
        "features": features,
    }


def _flowline_type(properties):
    ftype = properties.get("ftype")
    fcode = properties.get("fcode")
    try:
        if ftype is not None:
            return int(ftype)
        if fcode is not None:
            return int(fcode) // 100
    except (TypeError, ValueError):
        return None
    return None


def _is_candidate_flowline(properties):
    ftype = _flowline_type(properties)
    if ftype in EXCLUDED_FTYPES:
        return False
    if ftype is None:
        return True
    return ftype == STREAM_FTYPE


def _size_for_drop(drop):
    if drop >= 25:
        return "big"
    if drop >= 12:
        return "medium"
    return "small"


def _best_short_drop(zm, start_idx, long_k, short_k):
    end = min(start_idx + long_k - short_k, len(zm) - short_k - 1)
    if end < start_idx:
        return 0.0
    short_drops = [float(zm[i] - zm[i + short_k]) for i in range(start_idx, end + 1)]
    return max(short_drops) if short_drops else 0.0


def _profile_confidence_score(drop50, sharp_drop, upstream_km):
    if drop50 < MIN_PROFILE_DROP_M:
        return 0

    score = 0
    if drop50 >= 12:
        score += 1
    if drop50 >= 20:
        score += 1
    if drop50 >= 35:
        score += 1
    if drop50 >= 50:
        score += 1

    if sharp_drop >= max(MIN_SHARP_DROP_M, drop50 * MIN_SHARP_RATIO):
        score += 2
    elif sharp_drop >= MIN_SHARP_DROP_M:
        score += 1
    if sharp_drop >= 20:
        score += 1

    if upstream_km >= 8:
        score += 2
    elif upstream_km >= 3:
        score += 1

    return score


def _passes_profile_filter(drop50, sharp_drop, upstream_km):
    return _profile_confidence_score(drop50, sharp_drop, upstream_km) >= MIN_CONFIDENCE_SCORE


def _official_point_features(fc, lat, lon, radius_km, sample_elev=None):
    features = []
    for f in fc.get("features", []):
        coords = f.get("geometry", {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        try:
            point_lon, point_lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        dist_km = math.sqrt((point_lat - lat)**2 + (point_lon - lon)**2) * 111.0
        if dist_km > radius_km:
            continue
        props = f.get("properties", {})
        name = (props.get("gnis_name") or "Official waterfall/rapids").strip()
        elevation = None
        if sample_elev:
            try:
                elevation = round(float(sample_elev([point_lon], [point_lat])[0]))
            except (TypeError, ValueError, IndexError):
                elevation = None
        features.append({
            "lat": point_lat,
            "lon": point_lon,
            "stream": name,
            "drop_m": MIN_PROFILE_DROP_M,
            "elevation_m": elevation if elevation is not None else 0,
            "size": "medium",
            "upstream_km": None,
            "sharp_drop_m": None,
            "confidence": "official",
            "source": "nhd_official",
        })
    return features


def _dedupe_features_by_distance(features):
    deduped = []
    for f in sorted(features, key=lambda x: x.get("properties", {}).get("drop_m", 0), reverse=True):
        lon, lat = f["geometry"]["coordinates"][:2]
        duplicate = False
        for existing in deduped:
            e_lon, e_lat = existing["geometry"]["coordinates"][:2]
            if _feature_distance_m(lon, lat, e_lon, e_lat) < CACHE_DEDUPE_M:
                duplicate = True
                break
        if not duplicate:
            deduped.append(f)
    return deduped


def _grid_has_neighbor(grid, p, cell_size, merge_dist, items):
    gx, gy = int(p[0] / cell_size), int(p[1] / cell_size)
    span = int(math.ceil(merge_dist / cell_size)) + 1
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            for idx in grid.get((gx + dx, gy + dy), []):
                if np.hypot(*(p - items[idx]["_p"])) < merge_dist:
                    return True
    return False


def _grid_add(grid, p, cell_size, items):
    gx, gy = int(p[0] / cell_size), int(p[1] / cell_size)
    grid.setdefault((gx, gy), []).append(len(items) - 1)


def get_cached_results(lat, lon, radius_km):
    cache_key = results_cache_key(lat, lon, radius_km)
    results_path = Path(f"data/cache/results_{cache_key}.json")
    if results_path.exists():
        try:
            return _sanitize_cached_results(json.loads(results_path.read_text()), results_path)
        except (json.JSONDecodeError, OSError):
            try:
                results_path.unlink()
            except OSError:
                pass
    return None


def get_all_cached_features():
    features = []
    for path in sorted(Path("data/cache").glob("results_*.json")):
        try:
            fc = _sanitize_cached_results(json.loads(path.read_text()), path)
        except (json.JSONDecodeError, OSError):
            continue
        if not fc:
            continue
        for f in fc.get("features", []):
            coords = f.get("geometry", {}).get("coordinates")
            features.append(f)

    return _cache_result(_dedupe_features_by_distance(features))


def run_analysis(lat, lon, radius_km, on_progress=None):
    def report(pct, label):
        if on_progress:
            on_progress(pct, label)

    W, S, E, N = radius_bbox(lat, lon, radius_km)
    cache_key = results_cache_key(lat, lon, radius_km)
    results_path = f"data/cache/results_{cache_key}.json"

    cached = get_cached_results(lat, lon, radius_km)
    if cached is not None:
        print(f"Returning cached results for {cache_key}")
        report(100, "Using cached results")
        return cached

    utm_epsg = utm_crs(lat, lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True).transform
    to_ll  = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True).transform

    report(5, "Fetching stream network…")
    print(f"Fetching NHD flowlines...")
    flowlines_fc = fetch_flowlines(W, S, E, N, cache_key, on_progress=on_progress)
    official_fc = fetch_official_water_features(W, S, E, N, cache_key)

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
        props = f.get("properties", {})
        if not _is_candidate_flowline(props):
            continue
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
                "name": (props.get("gnis_name") or "").strip(),
                "ftype": props.get("ftype"),
                "fcode": props.get("fcode"),
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
        memo[si] = 0  # reserve slot to break cycles in the stream graph
        memo[si] = segs[si]["len"] + sum(
            upstream_len(j) for j in into.get(segs[si]["up"], []) if j != si
        )
        return memo[si]
    for si in range(len(segs)):
        upstream_len(si)

    # Scan elevation profiles for steep drops
    STEP_M   = 10.0
    WINDOW_M = 50
    k = int(WINDOW_M / STEP_M)
    short_k = max(1, int(SHORT_WINDOW_M / STEP_M))

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
        upstream_km = memo[si] / 1000.0
        for j in np.where(drops >= MIN_PROFILE_DROP_M)[0]:
            mid = j + k // 2
            pt_lon = float(lons_arr[mid])
            pt_lat = float(lats_arr[mid])
            # Filter to radius
            dist_km = math.sqrt((pt_lat - lat)**2 + (pt_lon - lon)**2) * 111.0
            if dist_km > radius_km:
                continue
            stream = s["name"]
            if not stream:
                continue
            sharp_drop = _best_short_drop(zm, j, k, short_k)
            confidence_score = _profile_confidence_score(float(drops[j]), sharp_drop, upstream_km)
            if confidence_score < MIN_CONFIDENCE_SCORE:
                continue
            hits.append({
                "si": si, "lat": pt_lat, "lon": pt_lon,
                "drop50": float(drops[j]), "elev": float(zm[mid]),
                "sharp_drop": sharp_drop, "upstream_km": upstream_km,
                "confidence_score": confidence_score,
            })

    print(f"  {len(hits)} raw hits")
    del flowlines_fc
    report(92, "Clustering waterfall candidates…")

    # Cluster within 120m (grid index avoids O(n^2) over large hit sets)
    hits.sort(key=lambda h: -h["drop50"])
    clusters = []
    cluster_grid = {}
    for h in hits:
        p = np.array(to_utm(h["lon"], h["lat"]))
        if _grid_has_neighbor(cluster_grid, p, 120, 120, clusters):
            continue
        s = segs[h["si"]]
        clusters.append({**h, "_p": p, "stream": s["name"],
                         "uplen_km": memo[h["si"]] / 1000.0})
        _grid_add(cluster_grid, p, 120, clusters)

    # Merge gorge clusters within 400m, apply min thresholds
    sites = []
    site_grid = {}
    for c in sorted(clusters, key=lambda x: -x["drop50"]):
        p = c["_p"]
        if _grid_has_neighbor(site_grid, p, 600, 600, sites):
            continue
        drop = round(c["drop50"], 1)
        sites.append({
            "lat": c["lat"],
            "lon": c["lon"],
            "stream": c["stream"],
            "drop_m": drop,
            "elevation_m": round(c["elev"]),
            "size": _size_for_drop(drop),
            "upstream_km": round(c["upstream_km"], 1),
            "sharp_drop_m": round(c["sharp_drop"], 1),
            "confidence": "high",
            "confidence_score": c["confidence_score"],
            "source": "dem_profile",
            "_p": p,
        })
        _grid_add(site_grid, p, 600, sites)

    sites.extend(_official_point_features(official_fc, lat, lon, radius_km, sample_elev=sample_elev))
    src_dem.close()
    del dem_data, src_dem

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
                "upstream_km": s["upstream_km"],
                "sharp_drop_m": s["sharp_drop_m"],
                "confidence": s["confidence"],
                "confidence_score": s.get("confidence_score"),
                "source": s["source"],
            },
        }
        for s in sorted(sites, key=lambda x: -x["drop_m"])
    ]
    result = _cache_result(_dedupe_features_by_distance(features))
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
        record_event("search_done", path=f"/search/{job_id}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_job(job_id, status="error", error=str(e))
        record_event("search_error", path=f"/search/{job_id}")


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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>Waterfall Finder</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet-geosearch@3.11.1/dist/geosearch.css"/>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }
    #toolbar {
      background: #1a1a2e; color: #eee; padding: 12px 14px;
      display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;
      box-shadow: 0 2px 6px rgba(0,0,0,0.4);
    }
    .toolbar-top {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
    }
    #toolbar h1 { font-size: 1.2rem; font-weight: 600; line-height: 1.2; }
    .toolbar-controls {
      display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
    }
    .toolbar-location {
      display: flex; flex-direction: column; gap: 2px; flex: 1 1 100%;
      min-width: 0;
    }
    #toolbar label {
      display: flex; align-items: center; gap: 8px;
      font-size: 0.9rem; color: #ccc; font-weight: 500;
    }
    #toolbar input[type=number] {
      width: 72px; min-height: 44px; padding: 8px 10px; border-radius: 8px;
      border: 1px solid #555; background: #2a2a3e; color: #eee; font-size: 1rem;
    }
    #coords-display { font-size: 0.88rem; color: #a8b4ff; }
    #search-btn, .complaints-link {
      min-height: 48px; padding: 12px 24px; background: #4f8ef7; color: #fff; border: none;
      border-radius: 10px; cursor: pointer; font-size: 1rem; font-weight: 600;
      flex: 1 1 auto;
    }
    #search-btn:hover { background: #3a7ae0; }
    #search-btn:disabled { background: #555; cursor: not-allowed; }
    .complaints-link {
      display: inline-flex; align-items: center; justify-content: center;
      background: transparent; border: 1px solid #6b7280; color: #eee;
      text-decoration: none; flex: 0 0 auto; margin-left: auto;
    }
    .complaints-link:hover { background: rgba(255,255,255,0.08); }
    #status {
      font-size: 0.85rem; color: #aaa; text-align: right;
      flex: 1 1 auto; min-width: 0;
    }
    .hint { font-size: 0.88rem; color: #bbb; }
    @media (max-width: 768px) {
      body {
        padding-bottom: calc(150px + env(safe-area-inset-bottom, 0px));
      }
      #toolbar {
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 1200;
        padding-bottom: calc(12px + env(safe-area-inset-bottom, 0px));
      }
      .complaints-link {
        position: absolute;
        top: 18px;
        right: 28px;
        min-height: auto;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        flex: 0 0 auto;
        margin-left: 0;
      }
      #status { text-align: left; }
      #map { min-height: 50vh; }
    }
    @media (min-width: 769px) {
      #toolbar {
        position: static;
        flex-direction: row; flex-wrap: wrap; align-items: center;
        padding: 10px 16px; gap: 14px;
      }
      .toolbar-top { flex: 0 0 auto; }
      .toolbar-controls { flex: 1 1 auto; flex-wrap: nowrap; gap: 16px; }
      .toolbar-location { flex: 0 1 auto; flex-direction: row; align-items: center; gap: 12px; }
      #toolbar h1 { font-size: 1.1rem; white-space: nowrap; }
      #toolbar input[type=number] { min-height: auto; padding: 4px 6px; font-size: 0.9rem; }
      #search-btn { min-height: auto; padding: 7px 18px; font-size: 0.9rem; border-radius: 5px; flex: 0 0 auto; }
      .complaints-link {
        min-height: auto; padding: 6px 12px; font-size: 0.85rem;
        border-radius: 5px; flex: 0 0 auto;
      }
      #status { text-align: left; }
      .hint { font-size: 0.78rem; white-space: nowrap; }
      #coords-display { font-size: 0.8rem; white-space: nowrap; }
    }
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
    .wf-marker { background: transparent; border: none; }
    .wf-hit {
      display: flex; align-items: center; justify-content: center;
      cursor: pointer;
    }
    .wf-dot {
      border-radius: 50%; border: 2px solid #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,.5);
      pointer-events: none;
    }
    .wf-center-dot {
      border-radius: 50%; background: #fff; border: 3px solid #333;
      box-shadow: 0 1px 4px rgba(0,0,0,.6);
      pointer-events: none;
    }
    .wf-popup { min-width: 180px; }
    .wf-popup b { font-size: 1rem; }
    .wf-popup .stat { color: #555; font-size: 0.85rem; margin-top: 2px; }
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
      margin-top: 18px; min-height: 48px; padding: 12px 24px; background: #4f8ef7; color: #fff;
      border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer;
      width: 100%;
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
  <div class="toolbar-top">
    <h1>💧 Waterfall Finder</h1>
    <span id="status"></span>
  </div>
  <div class="toolbar-controls">
    <div class="toolbar-location">
      <span class="hint">Tap the map to choose a search area</span>
      <span id="coords-display">No center set</span>
    </div>
    <label>Radius (km)
      <input type="number" id="radius" value="15" min="1" max="100" step="1"/>
    </label>
    <button id="search-btn" onclick="doSearch()">Search</button>
    <a class="complaints-link" href="https://waterfallfinder.featurebase.app/" target="_blank" rel="noopener noreferrer">Complaints</a>
  </div>
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
const DEFAULT_RADIUS_KM = 15;

function isCoarsePointer() {
  return window.matchMedia('(pointer: coarse)').matches || window.innerWidth <= 768;
}

function markerSizes() {
  if (isCoarsePointer()) return { hit: 44, dot: 26, center: 30 };
  return { hit: 22, dot: 14, center: 18 };
}

const map = L.map('map', {
  tapTolerance: isCoarsePointer() ? 25 : 15,
}).setView([DEFAULT_LAT, DEFAULT_LON], 11);

L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxZoom: 17,
}).addTo(map);

let centerMarker = null;
let radiusCircle = null;
let resultsLayer = L.featureGroup().addTo(map);
let centerLatLon = null;
let userHasSetCenter = false;
let allCachedFeatures = [];
const MIN_MARKER_ZOOM = 9;

function dotIcon(color) {
  const { hit, dot } = markerSizes();
  const anchor = hit / 2;
  return L.divIcon({
    className: 'wf-marker',
    html: `<div class="wf-hit" style="width:${hit}px;height:${hit}px">`
      + `<div class="wf-dot" style="width:${dot}px;height:${dot}px;background:${color}"></div></div>`,
    iconSize: [hit, hit],
    iconAnchor: [anchor, anchor],
  });
}

const sizeColors = { small: '#f1c40f', medium: '#e67e22', big: '#e74c3c' };

function clearSearchOverlay() {
  userHasSetCenter = false;
  centerLatLon = null;
  if (centerMarker) { map.removeLayer(centerMarker); centerMarker = null; }
  if (radiusCircle) { map.removeLayer(radiusCircle); radiusCircle = null; }
  document.getElementById('coords-display').textContent = 'No center set';
}

function setSearchCenter(latlng) {
  userHasSetCenter = true;
  centerLatLon = latlng;
  document.getElementById('coords-display').textContent =
    `${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`;

  if (centerMarker) map.removeLayer(centerMarker);
  const { hit, center } = markerSizes();
  const anchor = hit / 2;
  centerMarker = L.marker(latlng, {
    icon: L.divIcon({
      className: 'wf-marker',
      html: `<div class="wf-hit" style="width:${hit}px;height:${hit}px">`
        + `<div class="wf-center-dot" style="width:${center}px;height:${center}px"></div></div>`,
      iconSize: [hit, hit], iconAnchor: [anchor, anchor],
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

document.getElementById('radius').addEventListener('input', updateCircle);

function updateCircle() {
  if (!userHasSetCenter || !centerLatLon) return;
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

async function pollJob(job_id) {
  for (;;) {
    const res = await fetch(`/search/${job_id}/status`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || 'Search failed');
    }
    const job = await res.json();
    setProgress(job.pct, job.label);
    if (job.status === 'done') return job.result;
    if (job.status === 'error') throw new Error(job.error || 'Search failed');
    await new Promise(r => setTimeout(r, 500));
  }
}

function renderMarkers(features) {
  resultsLayer.clearLayers();
  features.forEach(f => {
    const p = f.properties;
    const color = sizeColors[p.size] || '#888';
    const marker = L.marker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {
      icon: dotIcon(color),
      zIndexOffset: p.size === 'big' ? 200 : p.size === 'medium' ? 100 : 0,
    });
    const lat = f.geometry.coordinates[1];
    const lon = f.geometry.coordinates[0];
    marker.bindPopup(`
      <div class="wf-popup">
        <b>${p.stream}</b>
        <div class="stat">Drop: <b>${p.drop_m} m</b> over 50 m window</div>
        <div class="stat">Elevation: ${p.elevation_m} m</div>
        <div class="stat">Size: likely ${p.size} waterfall</div>
        <div class="stat">${lat.toFixed(5)}, ${lon.toFixed(5)}</div>
        <div style="margin-top:6px">
          <a href="https://www.google.com/maps?q=${lat},${lon}" target="_blank">Open in Google Maps</a>
        </div>
      </div>
    `);
    resultsLayer.addLayer(marker);
  });
}

function visibleCachedFeatures() {
  if (map.getZoom() < MIN_MARKER_ZOOM) return [];
  const bounds = map.getBounds();
  return allCachedFeatures.filter(f => {
    const [lon, lat] = f.geometry.coordinates;
    return bounds.contains([lat, lon]);
  });
}

function updateVisibleMarkers() {
  renderMarkers(visibleCachedFeatures());
}

function fitFeatureBounds(features) {
  if (!features.length) return;
  const latlngs = features.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
  map.fitBounds(L.latLngBounds(latlngs).pad(0.2));
}

async function loadAllCached() {
  clearSearchOverlay();
  try {
    const res = await fetch('/cached/all');
    if (!res.ok) return;
    const fc = await res.json();
    allCachedFeatures = fc.features || [];
    updateVisibleMarkers();
  } catch (e) {}
}

map.on('moveend zoomend', updateVisibleMarkers);

window.addEventListener('resize', () => {
  map.options.tapTolerance = isCoarsePointer() ? 25 : 15;
  updateVisibleMarkers();
  if (userHasSetCenter && centerLatLon) setSearchCenter(centerLatLon);
});

loadAllCached();

async function doSearch() {
  if (!centerLatLon) { alert('Click the map to set a search center first.'); return; }
  const radius = parseFloat(document.getElementById('radius').value);
  const btn = document.getElementById('search-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  setProgress(2, 'Starting search…');

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

    const fc = await pollJob(job_id);
    const count = fc.features.length;
    status.textContent = `Found ${count} candidate${count !== 1 ? 's' : ''}.`;
    await loadAllCached();
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


def _stats_page(data):
    rows = "".join(
        f"<tr><td>{r['date']}</td>"
        f"<td>{r['human_visitors']}</td><td>{r['bot_visitors']}</td>"
        f"<td>{r['human_page_views']}</td><td>{r['bot_page_views']}</td>"
        f"<td>{r['human_preloads']}</td><td>{r['human_searches']}</td>"
        f"<td>{r['searches_done']}</td></tr>"
        for r in data["daily"]
    ) or "<tr><td colspan='8'>No traffic yet</td></tr>"
    recent = "".join(
        f"<li><code>{e['ts']}</code> "
        f"<span class='tag {e.get('kind', 'human')}'>{e.get('kind', 'human')}</span> "
        f"{e['event']} <span>{e['path']}</span></li>"
        for e in data["recent"]
    ) or "<li>No recent activity</li>"
    t = data["totals"]
    return f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8"/>
  <title>Waterfall Finder — Traffic</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #222; }}
    h1 {{ font-size: 1.4rem; }}
    h2 {{ font-size: 1.1rem; margin-top: 28px; }}
    table {{ border-collapse: collapse; margin: 16px 0 24px; font-size: 0.92rem; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }}
    .card {{ background: #f8f9fb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 16px; min-width: 120px; }}
    .card.humans {{ background: #eef4ff; border-color: #c7d9f7; }}
    .card.bots {{ background: #fff8ee; border-color: #f0d9a8; }}
    .card strong {{ display: block; font-size: 1.5rem; }}
    li {{ margin: 4px 0; }}
    li span {{ color: #666; }}
    .tag {{ display: inline-block; font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
            padding: 1px 6px; border-radius: 4px; color: #fff; }}
    .tag.human {{ background: #4f8ef7; }}
    .tag.bot {{ background: #c07a1a; }}
    .meta {{ color: #666; font-size: 0.9rem; }}
  </style>
</head><body>
  <h1>Waterfall Finder traffic</h1>
  <p class="meta">Updated {data['updated_at']} UTC · humans vs bots classified by User-Agent on each request</p>
  <h2>Humans</h2>
  <div class="cards">
    <div class="card humans"><strong>{t['human_visitors_today']}</strong> today</div>
    <div class="card humans"><strong>{t['new_human_visitors_today']}</strong> new today</div>
    <div class="card humans"><strong>{t['human_visitors_7d']}</strong> last 7 days</div>
    <div class="card humans"><strong>{t['human_visitors_all_time']}</strong> all time</div>
  </div>
  <h2>Bot scrapers</h2>
  <div class="cards">
    <div class="card bots"><strong>{t['bot_visitors_today']}</strong> today</div>
    <div class="card bots"><strong>{t['bot_visitors_7d']}</strong> last 7 days</div>
    <div class="card bots"><strong>{t['bot_visitors_all_time']}</strong> all time</div>
  </div>
  <h2>Activity</h2>
  <div class="cards">
    <div class="card humans"><strong>{t['human_page_views']}</strong> human page views</div>
    <div class="card bots"><strong>{t['bot_page_views']}</strong> bot page views</div>
    <div class="card humans"><strong>{t['human_preloads']}</strong> human preloads</div>
    <div class="card bots"><strong>{t['bot_preloads']}</strong> bot preloads</div>
    <div class="card humans"><strong>{t['human_searches']}</strong> human searches</div>
    <div class="card"><strong>{t['searches_done']}</strong> searches finished</div>
    <div class="card"><strong>{t['search_errors']}</strong> search errors</div>
  </div>
  <h2>Last 14 days</h2>
  <table>
    <tr><th>Date</th><th>Humans</th><th>Bots</th><th>Human views</th><th>Bot views</th>
        <th>Human preloads</th><th>Human searches</th><th>Finished</th></tr>
    {rows}
  </table>
  <h2>Recent activity</h2>
  <ul>{recent}</ul>
</body></html>"""


@app.route("/")
def index():
    record_event("page_view", path="/", ip=client_ip(), user_agent=client_user_agent())
    return HTML


@app.route("/stats")
def stats():
    return _stats_page(analytics_summary())


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_search_params(data):
    lat = float(data["lat"])
    lon = float(data["lon"])
    radius_km = float(data["radius_km"])
    if radius_km > 150:
        raise ValueError("Radius too large (max 150 km)")
    return lat, lon, radius_km


@app.route("/cached/all")
@limiter.limit("30 per minute")
def cached_all():
    record_event("preload", path="/cached/all", ip=client_ip(), user_agent=client_user_agent())
    return jsonify(get_all_cached_features())


@app.route("/cached")
@limiter.limit("30 per minute")
def cached():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius_km = float(request.args["radius_km"])
        if radius_km > 150:
            return jsonify({"error": "Radius too large (max 150 km)"}), 400
    except (TypeError, ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400

    result = get_cached_results(lat, lon, radius_km)
    if result is None:
        return jsonify({"error": "No cached results"}), 404
    record_event("preload", path="/cached", ip=client_ip(), user_agent=client_user_agent())
    return jsonify(result)


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
    record_event("search", path="/search", ip=ip, user_agent=client_user_agent())
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


@app.route("/search/<job_id>/status")
@limiter.limit("120 per minute")
def search_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    payload = {
        "status": job["status"],
        "pct": job["pct"],
        "label": job["label"],
    }
    if job["status"] == "done":
        payload["result"] = job["result"]
    elif job["status"] == "error":
        payload["error"] = job["error"]
    return jsonify(payload)


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
