import analytics


def test_record_increments_totals(tmp_path, monkeypatch):
    path = tmp_path / "analytics.json"
    monkeypatch.setattr(analytics, "ANALYTICS_PATH", path)

    analytics.record("page_view", path="/", ip="1.2.3.4", user_agent="Mozilla/5.0 Safari")
    analytics.record("search", path="/search", ip="1.2.3.4", user_agent="Mozilla/5.0 Safari")

    data = analytics.summary()
    assert data["totals"]["page_views"] == 1
    assert data["totals"]["searches"] == 1
    assert data["totals"]["human_visitors_today"] == 1
    assert data["totals"]["human_visitors_all_time"] == 1
    assert data["totals"]["new_human_visitors_today"] == 1
    assert data["totals"]["bot_visitors_today"] == 0
    assert len(data["daily"]) == 1
    assert data["daily"][0]["human_visitors"] == 1
    assert len(data["recent"]) == 2


def test_unique_visitors_deduped_by_ip(tmp_path, monkeypatch):
    path = tmp_path / "analytics.json"
    monkeypatch.setattr(analytics, "ANALYTICS_PATH", path)

    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/605.1.15"
    analytics.record("page_view", path="/", ip="1.2.3.4", user_agent=ua)
    analytics.record("page_view", path="/", ip="1.2.3.4", user_agent=ua)
    analytics.record("page_view", path="/", ip="5.6.7.8", user_agent=ua)

    data = analytics.summary()
    assert data["totals"]["page_views"] == 3
    assert data["totals"]["human_visitors_today"] == 2
    assert data["totals"]["human_visitors_all_time"] == 2


def test_bot_user_agents_are_classified(tmp_path, monkeypatch):
    path = tmp_path / "analytics.json"
    monkeypatch.setattr(analytics, "ANALYTICS_PATH", path)

    analytics.record("page_view", path="/", ip="9.9.9.9", user_agent="python-requests/2.31")
    analytics.record("page_view", path="/", ip="8.8.8.8", user_agent="Mozilla/5.0 Chrome/120")
    analytics.record("page_view", path="/", ip="7.7.7.7", user_agent="")

    data = analytics.summary()
    assert data["totals"]["bot_visitors_today"] == 2
    assert data["totals"]["human_visitors_today"] == 1
    assert data["totals"]["bot_page_views"] == 2
    assert data["totals"]["human_page_views"] == 1
    assert data["recent"][0]["kind"] == "bot"


def test_is_bot_detects_common_scrapers():
    assert analytics.is_bot("python-requests/2.31")
    assert analytics.is_bot("Googlebot/2.1")
    assert analytics.is_bot("curl/8.0")
    assert not analytics.is_bot("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")
