import hashlib
import json
from unittest.mock import patch

import pytest

from server import (
    _sse,
    app,
    dem_tile_urls,
    radius_bbox,
    run_analysis,
    utm_crs,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestHelpers:
    def test_radius_bbox_contains_center(self):
        lat, lon, radius = 42.7, -74.4, 15
        W, S, E, N = radius_bbox(lat, lon, radius)
        assert S < lat < N
        assert W < lon < E

    def test_radius_bbox_grows_with_radius(self):
        small = radius_bbox(42.0, -74.0, 5)
        large = radius_bbox(42.0, -74.0, 20)
        assert (large[2] - large[0]) > (small[2] - small[0])
        assert (large[3] - large[1]) > (small[3] - small[1])

    def test_utm_crs_northern_hemisphere(self):
        assert utm_crs(42.7, -74.4) == 32618

    def test_utm_crs_southern_hemisphere(self):
        assert utm_crs(-33.9, 151.2) == 32756

    def test_dem_tile_urls_single_tile(self):
        urls = dem_tile_urls(-74.9, 42.2, -74.2, 42.8)
        assert len(urls) == 1
        assert urls[0].endswith("USGS_13_n43w075.tif")

    def test_dem_tile_urls_multiple_tiles(self):
        urls = dem_tile_urls(-75.1, 42.1, -74.1, 43.1)
        assert len(urls) == 4
        assert all("prd-tnm.s3.amazonaws.com" in u for u in urls)
        assert any(u.endswith("USGS_13_n43w075.tif") for u in urls)


class TestRoutes:
    def test_index_returns_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert b"Waterfall Finder" in res.data
        assert b"progress-track" in res.data

    def test_search_rejects_large_radius(self, client):
        res = client.post(
            "/search",
            json={"lat": 42.7, "lon": -74.4, "radius_km": 200},
        )
        assert res.status_code == 400
        assert "too large" in res.get_json()["error"].lower()

    def test_search_rejects_bad_payload(self, client):
        res = client.post("/search", json={"lat": "not-a-number"})
        assert res.status_code == 400

    @patch("server.run_analysis")
    def test_search_returns_geojson(self, mock_run, client):
        mock_run.return_value = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-74.4, 42.7]},
                    "properties": {
                        "stream": "Test Creek",
                        "drop_m": 12.0,
                        "elevation_m": 300,
                        "size": "medium",
                    },
                }
            ],
        }
        res = client.post(
            "/search",
            json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1
        mock_run.assert_called_once_with(42.7, -74.4, 15)

    @patch("server.run_analysis")
    def test_search_streams_progress_events(self, mock_run, client):
        def fake_analysis(lat, lon, radius_km, on_progress=None):
            if on_progress:
                on_progress(10, "Fetching stream network…")
                on_progress(100, "Done")
            return {"type": "FeatureCollection", "features": []}

        mock_run.side_effect = fake_analysis

        res = client.post(
            "/search",
            headers={"Accept": "text/event-stream"},
            json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
        )
        assert res.status_code == 200
        body = res.data.decode()
        assert "event: progress" in body
        assert "event: result" in body
        assert "Fetching stream network" in body


class TestRunAnalysis:
    def test_cached_results_skip_fetch(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "data" / "cache"
        cache_dir.mkdir(parents=True)

        lat, lon, radius = 42.7, -74.4, 15
        W, S, E, N = radius_bbox(lat, lon, radius)
        bbox_sig = f"{W:.3f}{S:.3f}{E:.3f}{N:.3f}"
        cache_key = hashlib.md5(bbox_sig.encode()).hexdigest()[:10]

        cached = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-74.4, 42.7]},
                    "properties": {
                        "stream": "Cached Creek",
                        "drop_m": 8.0,
                        "elevation_m": 250,
                        "size": "small",
                    },
                }
            ],
        }
        results_path = cache_dir / f"results_{cache_key}.json"
        results_path.write_text(json.dumps(cached))

        monkeypatch.chdir(tmp_path)
        progress = []
        result = run_analysis(lat, lon, radius, on_progress=lambda p, l: progress.append((p, l)))

        assert result == cached
        assert progress == [(100, "Using cached results")]


class TestSse:
    def test_sse_format(self):
        msg = _sse("progress", {"pct": 50, "label": "Working"})
        assert msg.startswith("event: progress\n")
        assert '"pct": 50' in msg
        assert msg.endswith("\n\n")
