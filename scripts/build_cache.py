#!/usr/bin/env python3
"""Build waterfall results cache for an area. Run offline; commit results to git."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (  # noqa: E402
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_RADIUS_KM,
    get_cached_results,
    results_cache_key,
    run_analysis,
)

MANIFEST_PATH = Path("data/cache/manifest.json")


def load_manifest():
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"entries": []}


def save_manifest(manifest):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def update_manifest(lat, lon, radius_km, feature_count):
    key = results_cache_key(lat, lon, radius_km)
    manifest = load_manifest()
    entry = {
        "key": key,
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "features": feature_count,
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    entries = [e for e in manifest.get("entries", []) if e.get("key") != key]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("built_at", ""), reverse=True)
    manifest["entries"] = entries
    save_manifest(manifest)


def build(lat, lon, radius_km, force=False):
    key = results_cache_key(lat, lon, radius_km)
    if not force and get_cached_results(lat, lon, radius_km):
        cached = get_cached_results(lat, lon, radius_km)
        count = len(cached.get("features", []))
        print(f"Cache already exists for {key} ({count} features). Use --force to rebuild.")
        update_manifest(lat, lon, radius_km, count)
        return cached

    print(f"Building cache {key} — {lat}, {lon}, {radius_km} km…")
    result = run_analysis(lat, lon, radius_km)
    count = len(result.get("features", []))
    update_manifest(lat, lon, radius_km, count)
    print(f"Done: {count} features → data/cache/results_{key}.json")
    return result


def main():
    parser = argparse.ArgumentParser(description="Build offline waterfall cache for an area.")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--force", action="store_true", help="Rebuild even if results exist")
    args = parser.parse_args()
    build(args.lat, args.lon, args.radius_km, force=args.force)


if __name__ == "__main__":
    main()
