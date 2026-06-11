import analytics


def test_record_increments_totals(tmp_path, monkeypatch):
    path = tmp_path / "analytics.json"
    monkeypatch.setattr(analytics, "ANALYTICS_PATH", path)

    analytics.record("page_view", path="/", ip="1.2.3.4")
    analytics.record("search", path="/search", ip="1.2.3.4")

    data = analytics.summary()
    assert data["totals"]["page_views"] == 1
    assert data["totals"]["searches"] == 1
    assert len(data["daily"]) == 1
    assert data["daily"][0]["unique_visitors"] == 1
    assert len(data["recent"]) == 2
