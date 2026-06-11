import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_cache


def test_build_updates_manifest(tmp_path, monkeypatch):
    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_cache, "MANIFEST_PATH", cache_dir / "manifest.json")

    key = "abc123"
    results = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature"}],
    }
    (cache_dir / f"results_{key}.json").write_text(json.dumps(results))
    monkeypatch.setattr(
        build_cache,
        "results_cache_key",
        lambda lat, lon, radius: key,
    )
    monkeypatch.setattr(build_cache, "get_cached_results", lambda lat, lon, radius: results)

    build_cache.build(42.0, -74.0, 15)

    manifest = json.loads((cache_dir / "manifest.json").read_text())
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["key"] == key
    assert manifest["entries"][0]["features"] == 1
