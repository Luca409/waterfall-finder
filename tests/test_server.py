import hashlib
import json
import time
from unittest.mock import patch

import pytest

import analytics
from server import (
    _sse,
    app,
    create_job,
    dem_tile_urls,
    get_all_cached_features,
    get_cached_results,
    get_job,
    radius_bbox,
    results_cache_key,
    run_analysis,
    start_job,
    utm_crs,
)


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "data" / "cache" / "jobs"
    jobs_dir.mkdir(parents=True)
    monkeypatch.setattr("server.JOBS_DIR", jobs_dir)
    monkeypatch.setattr(analytics, "ANALYTICS_PATH", tmp_path / "analytics.json")
    return jobs_dir


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
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

    def test_search_returns_job_id(self, client):
        with patch("server.start_job"):
            res = client.post(
                "/search",
                json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
            )
        assert res.status_code == 202
        assert "job_id" in res.get_json()

    @patch("server.run_analysis")
    def test_search_wait_returns_geojson(self, mock_run, client):
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
            "/search?wait=1",
            headers={"Accept": "application/json"},
            json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1
        mock_run.assert_called_once()
        assert mock_run.call_args[0][:3] == (42.7, -74.4, 15)

    @patch("server.run_analysis")
    def test_search_polls_job_status(self, mock_run, client):
        def fake_analysis(lat, lon, radius_km, on_progress=None):
            if on_progress:
                on_progress(10, "Fetching stream network…")
                on_progress(100, "Done")
            return {"type": "FeatureCollection", "features": []}

        mock_run.side_effect = fake_analysis

        start = client.post(
            "/search",
            json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
        )
        assert start.status_code == 202
        job_id = start.get_json()["job_id"]

        for _ in range(50):
            res = client.get(f"/search/{job_id}/status")
            assert res.status_code == 200
            job = res.get_json()
            if job["status"] == "done":
                assert job["result"]["type"] == "FeatureCollection"
                return
            time.sleep(0.05)
        raise AssertionError("job did not complete")

    @patch("server.run_analysis")
    def test_search_events_404_for_unknown_job(self, mock_run, client):
        res = client.get("/search/doesnotexist/events")
        assert res.status_code == 404

    def test_cached_returns_results(self, client, isolated_jobs, monkeypatch):
        monkeypatch.chdir(isolated_jobs.parent.parent.parent)
        cache_dir = isolated_jobs.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        lat, lon, radius = 42.42457, -74.40353, 30
        cache_key = results_cache_key(lat, lon, radius)
        cached = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-74.4, 42.4]},
                    "properties": {
                        "stream": "Test Creek",
                        "drop_m": 10.0,
                        "elevation_m": 300,
                        "size": "small",
                    },
                }
            ],
        }
        (cache_dir / f"results_{cache_key}.json").write_text(json.dumps(cached))

        res = client.get(f"/cached?lat={lat}&lon={lon}&radius_km={radius}")
        assert res.status_code == 200
        assert len(res.get_json()["features"]) == 1

    def test_cached_404_when_missing(self, client):
        res = client.get("/cached?lat=1&lon=2&radius_km=30")
        assert res.status_code == 404

    def test_cached_all_merges_results(self, client, isolated_jobs, monkeypatch):
        monkeypatch.chdir(isolated_jobs.parent.parent.parent)
        cache_dir = isolated_jobs.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        for i, lon in enumerate([-74.4, -74.5]):
            fc = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, 42.4 + i * 0.1]},
                    "properties": {"stream": f"Creek {i}", "drop_m": 10, "elevation_m": 300, "size": "small"},
                }],
            }
            (cache_dir / f"results_key{i}.json").write_text(json.dumps(fc))

        res = client.get("/cached/all")
        assert res.status_code == 200
        assert len(res.get_json()["features"]) == 2

    def test_get_all_cached_features_dedupes(self, isolated_jobs, monkeypatch):
        cache_dir = isolated_jobs.parent
        monkeypatch.chdir(isolated_jobs.parent.parent.parent)
        dup = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-74.4, 42.4]},
                "properties": {"stream": "A", "drop_m": 10, "elevation_m": 300, "size": "small"},
            }],
        }
        (cache_dir / "results_a.json").write_text(json.dumps(dup))
        (cache_dir / "results_b.json").write_text(json.dumps(dup))
        assert len(get_all_cached_features()["features"]) == 1

    def test_stats_is_public(self, client):
        res = client.get("/stats")
        assert res.status_code == 200
        assert "Waterfall Finder traffic" in res.get_data(as_text=True)


class TestRunAnalysis:
    def test_cached_results_skip_fetch(self, tmp_path, monkeypatch, isolated_jobs):
        cache_dir = isolated_jobs.parent

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


class TestRateLimit:
    def test_search_rate_limited(self, client):
        from server import limiter

        app.config["RATELIMIT_ENABLED"] = True
        limiter.storage.storage.clear()
        headers = {"X-Forwarded-For": "10.0.0.1"}
        with patch("server.start_job"), patch("server.active_jobs_for_ip", return_value=0):
            for _ in range(5):
                res = client.post(
                    "/search",
                    json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
                    headers=headers,
                )
                assert res.status_code == 202
            res = client.post(
                "/search",
                json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
                headers=headers,
            )
            assert res.status_code == 429
        app.config["RATELIMIT_ENABLED"] = False

    def test_concurrent_jobs_limited(self, client, isolated_jobs):
        for i in range(1):
            job = {
                "id": f"job{i}",
                "status": "running",
                "client_ip": "10.0.0.2",
                "pct": 50,
                "label": "Working",
            }
            (isolated_jobs / f"job{i}.json").write_text(json.dumps(job))

        with patch("server.start_job"):
            res = client.post(
                "/search",
                json={"lat": 42.7, "lon": -74.4, "radius_km": 15},
                headers={"X-Forwarded-For": "10.0.0.2"},
            )
        assert res.status_code == 429
        assert "in progress" in res.get_json()["error"].lower()


class TestJobs:
    @patch("server.run_analysis")
    def test_job_runs_in_background(self, mock_run):
        mock_run.return_value = {"type": "FeatureCollection", "features": []}

        job_id = create_job(42.7, -74.4, 15)
        start_job(job_id, 42.7, -74.4, 15)

        for _ in range(50):
            job = get_job(job_id)
            if job["status"] == "done":
                break
            time.sleep(0.05)
        assert job["status"] == "done"
        assert job["result"]["type"] == "FeatureCollection"


class TestSse:
    def test_sse_format(self):
        msg = _sse("progress", {"pct": 50, "label": "Working"})
        assert msg.startswith("event: progress\n")
        assert '"pct": 50' in msg
        assert msg.endswith("\n\n")
