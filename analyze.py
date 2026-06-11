#!/usr/bin/env python3
"""
Schoharie County Waterfall Finder
==================================
Detects waterfall candidates from first principles using:
  - USGS 3DEP 1/3 arc-second (10m) Digital Elevation Model
  - USGS National Hydrography Dataset (NHD) flowlines & waterbodies
  - US Census TIGERweb county boundary

No social media, Google Maps, or crowd-sourced data used.

Usage:
    pip install rasterio shapely pyproj
    python analyze.py

Outputs:
    data/               -- cached geodata (flowlines, DEM, etc.)
    schoharie_waterfall_candidates.csv  -- ranked candidate sites
"""

import os, json, math, csv, time, urllib.request, urllib.parse
import numpy as np
import rasterio
from rasterio.windows import from_bounds
from shapely.geometry import shape, Point, MultiLineString, mapping
from shapely.ops import transform as shp_transform
from shapely.prepared import prep
from pyproj import Transformer

os.makedirs("data", exist_ok=True)
os.environ["GDAL_HTTP_UNSAFESSL"] = "YES"  # needed behind some corporate proxies

# ---------------------------------------------------------------------------
# 1. County boundary
# ---------------------------------------------------------------------------
COUNTY_FILE = "data/county.geojson"

def fetch_county():
    print("Fetching Schoharie County boundary...")
    url = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/11/query"
    params = {
        "where": "STATE='36' AND NAME='Schoharie County'",
        "outFields": "NAME,STATE,COUNTY",
        "returnGeometry": "true", "outSR": "4326", "f": "geojson"
    }
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "research"})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    geom = data["features"][0]["geometry"]
    json.dump(geom, open(COUNTY_FILE, "w"))
    return geom

if os.path.exists(COUNTY_FILE):
    county_geom = json.load(open(COUNTY_FILE))
else:
    county_geom = fetch_county()

county = shape(county_geom)
pcounty = prep(county)
W, S, E, N = county.bounds
BBOX = f"{W:.4f},{S:.4f},{E:.4f},{N:.4f}"
print(f"County bounds: {BBOX}")

# ---------------------------------------------------------------------------
# 2. NHD Flowlines (layer 6)
# ---------------------------------------------------------------------------
FLOW_FILE = "data/flowlines.geojson"

def fetch_flowlines():
    print("Fetching NHD flowlines (paginated)...")
    base = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
    all_feats = []
    offset = 0
    while True:
        params = {
            "geometry": BBOX, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "gnis_name,ftype,fcode,lengthkm",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson",
            "resultOffset": offset, "resultRecordCount": 1000
        }
        req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "research"})
        data = json.load(urllib.request.urlopen(req, timeout=120))
        feats = data.get("features", [])
        all_feats.extend(feats)
        print(f"  offset {offset}: +{len(feats)} (total {len(all_feats)})")
        if len(feats) < 1000:
            break
        offset += 1000
        time.sleep(0.5)
    # Clip to county
    kept = []
    for f in all_feats:
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        if pcounty.intersects(g):
            gi = g.intersection(county)
            if gi.is_empty:
                continue
            f["geometry"] = mapping(gi)
            kept.append(f)
    print(f"  {len(kept)} flowlines within county")
    fc = {"type": "FeatureCollection", "features": kept}
    json.dump(fc, open(FLOW_FILE, "w"))
    return fc

if os.path.exists(FLOW_FILE):
    flowlines_fc = json.load(open(FLOW_FILE))
else:
    flowlines_fc = fetch_flowlines()

# ---------------------------------------------------------------------------
# 3. NHD Waterbodies (layer 9) -- used to flag dams/reservoirs
# ---------------------------------------------------------------------------
WB_FILE = "data/waterbodies.geojson"

def fetch_waterbodies():
    print("Fetching NHD waterbodies...")
    base = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/9/query"
    params = {
        "geometry": BBOX, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "gnis_name,ftype,areasqkm",
        "returnGeometry": "true", "outSR": "4326", "f": "geojson", "resultRecordCount": 2000
    }
    req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "research"})
    wb = json.load(urllib.request.urlopen(req, timeout=120))
    json.dump(wb, open(WB_FILE, "w"))
    return wb

if os.path.exists(WB_FILE):
    wb_fc = json.load(open(WB_FILE))
else:
    wb_fc = fetch_waterbodies()

# ---------------------------------------------------------------------------
# 4. USGS 3DEP 1/3 arc-second DEM
# ---------------------------------------------------------------------------
DEM_FILE = "data/dem.tif"

def fetch_dem():
    print("Downloading USGS 1/3\" DEM tile (this may take a moment)...")
    url = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
           "current/n43w075/USGS_13_n43w075.tif")
    margin = 0.02
    with rasterio.open(url) as src:
        win = from_bounds(W - margin, S - margin, E + margin, N + margin, src.transform)
        data = src.read(1, window=win)
        transform = src.window_transform(win)
        profile = src.profile.copy()
        profile.update(height=data.shape[0], width=data.shape[1],
                       transform=transform, compress="deflate")
        with rasterio.open(DEM_FILE, "w", **profile) as dst:
            dst.write(data, 1)
    print(f"  DEM saved: {data.shape}, elevation {np.nanmin(data):.0f}–{np.nanmax(data):.0f} m")

if not os.path.exists(DEM_FILE):
    fetch_dem()

src_dem = rasterio.open(DEM_FILE)
dem = src_dem.read(1)
inv_transform = ~src_dem.transform

# ---------------------------------------------------------------------------
# 5. Coordinate helpers
# ---------------------------------------------------------------------------
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:26918", always_xy=True).transform
to_ll  = Transformer.from_crs("EPSG:26918", "EPSG:4326", always_xy=True).transform

def sample_elev(lons, lats):
    cols, rows = inv_transform * (np.asarray(lons), np.asarray(lats))
    rows = np.clip(rows.astype(int), 0, dem.shape[0] - 1)
    cols = np.clip(cols.astype(int), 0, dem.shape[1] - 1)
    return dem[rows, cols]

# ---------------------------------------------------------------------------
# 6. Build stream-network graph (upstream channel length per segment)
# ---------------------------------------------------------------------------
print("Building stream network...")
segs = []
for f in flowlines_fc["features"]:
    g = shape(f["geometry"])
    for g1 in (g.geoms if isinstance(g, MultiLineString) else [g]):
        gu = shp_transform(to_utm, g1)
        if gu.length < 20:
            continue
        (x0, y0), (x1, y1) = gu.coords[0], gu.coords[-1]
        lon0, lat0 = to_ll(x0, y0)
        lon1, lat1 = to_ll(x1, y1)
        z0 = float(sample_elev([lon0], [lat0])[0])
        z1 = float(sample_elev([lon1], [lat1])[0])
        key = lambda p: (round(p[0] / 20), round(p[1] / 20))
        up, dn = ((x0, y0), (x1, y1)) if z0 >= z1 else ((x1, y1), (x0, y0))
        segs.append({
            "geom": gu, "len": gu.length,
            "up": key(up), "dn": key(dn),
            "name": (f["properties"].get("gnis_name") or "").strip(),
            "fcode": f["properties"].get("fcode")
        })

into = {}
for si, s in enumerate(segs):
    into.setdefault(s["dn"], []).append(si)

import sys; sys.setrecursionlimit(200000)
memo = {}

def upstream_len(si):
    if si in memo:
        return memo[si]
    memo[si] = 0.0
    s = segs[si]
    memo[si] = s["len"] + sum(upstream_len(j) for j in into.get(s["up"], []) if j != si)
    return memo[si]

for si in range(len(segs)):
    upstream_len(si)
print(f"  {len(segs)} segments, network lengths computed")

# ---------------------------------------------------------------------------
# 7. Scan profiles for steep drops
# ---------------------------------------------------------------------------
STEP_M    = 10.0   # profile sample spacing (m)
WINDOW_M  = 50     # sliding window length (m)
MIN_DROP  = 4.0    # minimum vertical drop to record (m)

print("Scanning stream elevation profiles...")
hits = []
k = int(WINDOW_M / STEP_M)

for si, s in enumerate(segs):
    gu = s["geom"]
    n = max(int(gu.length / STEP_M) + 1, 7)
    dists = np.linspace(0, gu.length, n)
    pts = [gu.interpolate(d) for d in dists]
    xs = np.array([p.x for p in pts])
    ys = np.array([p.y for p in pts])
    lons, lats = to_ll(xs, ys)
    z = sample_elev(lons, lats).astype(float)
    if z[-1] > z[0]:          # ensure upstream→downstream orientation
        z    = z[::-1]
        lons = lons[::-1]
        lats = lats[::-1]
    zm = np.minimum.accumulate(z)   # monotone envelope (kills DEM noise)
    if len(zm) <= k:
        continue
    drops = zm[:-k] - zm[k:]
    for j in np.where(drops >= MIN_DROP)[0]:
        mid = j + k // 2
        hits.append({
            "si": si,
            "lat": float(lats[mid]), "lon": float(lons[mid]),
            "drop50": float(drops[j]),
            "elev": float(zm[mid])
        })

print(f"  {len(hits)} raw hits")

# ---------------------------------------------------------------------------
# 8. Cluster hits within 120 m → one site per waterfall
# ---------------------------------------------------------------------------
hits.sort(key=lambda h: -h["drop50"])
clusters = []
for h in hits:
    p = np.array(to_utm(h["lon"], h["lat"]))
    placed = False
    for c in clusters:
        if np.hypot(*(p - c["_p"])) < 120:
            placed = True
            break
    if not placed:
        s = segs[h["si"]]
        clusters.append({**h, "_p": p,
                         "stream": s["name"],
                         "fcode": s["fcode"],
                         "uplen_km": memo[h["si"]] / 1000.0})

print(f"  {len(clusters)} clusters after spatial deduplication")

# ---------------------------------------------------------------------------
# 9. Merge gorge-clusters within 400 m, flag reservoir dams, name streams
# ---------------------------------------------------------------------------
named_segs = [(s["name"], shp_transform(to_utm, shape(f["geometry"])))
              for s, f in zip(segs, flowlines_fc["features"])
              if s["name"]]

wbs_utm = [(f["properties"].get("gnis_name"),
            f["properties"].get("areasqkm") or 0,
            shp_transform(to_utm, shape(f["geometry"])))
           for f in wb_fc.get("features", [])]

def resolve_name(c):
    p = Point(to_utm(c["lon"], c["lat"]))
    for nm, area, g in wbs_utm:
        if area > 0.2 and g.distance(p) < 300:
            return None, f"dam/reservoir ({nm or 'unnamed'})"
    if c["stream"]:
        return c["stream"], None
    best = (1e9, None)
    for nm, g in named_segs:
        d = g.distance(p)
        if d < best[0]:
            best = (d, nm)
    if best[0] < 2500:
        return f"unnamed trib. of {best[1]}", None
    return "unnamed stream", None

clusters.sort(key=lambda c: -c["drop50"])
sites = []
for c in clusters:
    if c["drop50"] < 6:
        continue
    p = np.array(to_utm(c["lon"], c["lat"]))
    merged = False
    for s in sites:
        if np.hypot(*(p - s["_p"])) < 400:
            merged = True
            break
    if merged:
        continue
    nm, dam = resolve_name(c)
    sites.append({**c, "stream": nm, "dam": dam})

real = [s for s in sites if not s["dam"]]
dams = [s for s in sites if s["dam"]]
major  = [s for s in real if s["uplen_km"] >= 8]
medium = [s for s in real if 2 <= s["uplen_km"] < 8 and s["drop50"] >= 10]

print(f"\n{'='*60}")
print(f"Major creek falls (upstream ≥8 km): {len(major)}")
print(f"Ravine/tributary falls (drop ≥10 m, 2–8 km): {len(medium)}")
print(f"Dam artifacts excluded: {len(dams)}")

# ---------------------------------------------------------------------------
# 10. Write CSV
# ---------------------------------------------------------------------------
OUTPUT = "schoharie_waterfall_candidates.csv"
rows = []
for s in sorted(major, key=lambda s: -s["drop50"]):
    rows.append(["major creek", s["stream"],
                 round(s["lat"], 5), round(s["lon"], 5),
                 round(s["drop50"], 1), round(s["uplen_km"], 1),
                 round(s["elev"]), ""])
for s in sorted(medium, key=lambda s: -s["drop50"]):
    rows.append(["ravine/trib", s["stream"],
                 round(s["lat"], 5), round(s["lon"], 5),
                 round(s["drop50"], 1), round(s["uplen_km"], 1),
                 round(s["elev"]), ""])
for s in dams:
    rows.append(["excluded-dam", s["dam"],
                 round(s["lat"], 5), round(s["lon"], 5),
                 round(s["drop50"], 1), round(s["uplen_km"], 1),
                 round(s["elev"]), "dam"])

with open(OUTPUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["class", "stream", "latitude", "longitude",
                "drop_m_per_50m", "upstream_channel_km", "elevation_m", "flag"])
    w.writerows(rows)

print(f"\nWrote {len(rows)} sites → {OUTPUT}")
print("\nTop 10 candidates:")
for r in rows[:10]:
    print(f"  {r[1]:<45} {r[2]},{r[3]}  drop={r[4]}m  up={r[5]}km")
