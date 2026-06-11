# Schoharie County Waterfall Finder

Detects waterfall candidates from first principles — **no Google Maps, no social media, no crowd-sourced data**. Everything is derived from official USGS elevation and hydrography data.

## Method

1. **County boundary** — US Census TIGERweb
2. **Stream network** — USGS National Hydrography Dataset (NHD) flowlines, all 3,782 segments clipped to the county
3. **Elevation** — USGS 3DEP 1/3 arc-second (≈10m) DEM, tile `n43w075`
4. **Detection** — Each stream segment is sampled every 10m; a monotone-envelope profile kills DEM noise, then a 50m sliding window flags spots where the stream drops ≥4m
5. **Clustering** — Raw hits within 120m are merged into one site; gorge-sequences within 400m are collapsed
6. **Classification** — Sites scored by upstream-channel length (proxy for water volume) and drop magnitude; reservoir/dam adjacency flagged and excluded

## Results summary

| Class | Count |
|---|---|
| Major creek falls (upstream ≥8 km network) | 24 |
| Ravine/tributary falls (≥10 m drop, 2–8 km) | 104 |
| Dam artifacts excluded | — |

Notable verified hits (algorithm independently found these):
- **Mine Kill Falls** — 18 m/50 m on an 86 km network
- **Panther Creek / Bouck's Falls area** — 22 m/50 m
- **Keyser Kill gorge** — 22 m/50 m on 43 km network
- **Platter Kill gorge** — 14 m/50 m near Gilboa

## Usage

```bash
pip install rasterio shapely pyproj
python analyze.py
```

First run downloads ~200 MB of geodata into `data/`. Subsequent runs use the cache.

Output: `schoharie_waterfall_candidates.csv`

## Output columns

| Column | Description |
|---|---|
| `class` | `major creek` or `ravine/trib` |
| `stream` | Named stream or "unnamed trib. of X" |
| `latitude` / `longitude` | WGS84 centroid of the drop |
| `drop_m_per_50m` | Vertical drop over 50m horizontal window |
| `upstream_channel_km` | Total upstream channel length (water volume proxy) |
| `elevation_m` | Approximate elevation of the drop |
| `flag` | `dam` if excluded as artificial |

## Caveats

- 10m DEM cannot resolve falls shorter than ~3–4m
- A steep cascade and a single plunge look the same within one 50m window
- Small mill dams may register as falls if not in the NHD waterbody layer
- Seasonal / intermittent streams are included — some sites may be dry in summer
- **Ground-truthing required before any field visit**
