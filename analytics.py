import hashlib
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ANALYTICS_PATH = Path(os.environ.get("ANALYTICS_PATH", "data/cache/analytics.json"))
_STATS_SALT = os.environ.get("STATS_SALT", "waterfall-finder")
_LOCK = threading.Lock()
_RECENT_LIMIT = 50
_DAILY_RETENTION_DAYS = 90


def _empty():
    return {"totals": {}, "daily": {}, "all_time_visitors": {}, "recent": []}


def _load():
    if ANALYTICS_PATH.exists():
        try:
            return json.loads(ANALYTICS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return _empty()


def _save(data):
    ANALYTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANALYTICS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _prune_daily(daily):
    cutoff = date.today() - timedelta(days=_DAILY_RETENTION_DAYS)
    for day in list(daily):
        if day < cutoff.isoformat():
            del daily[day]


def visitor_hash(ip):
    return hashlib.sha256(f"{_STATS_SALT}:{ip}".encode()).hexdigest()[:12]


def record(event, path="", ip=""):
    today = date.today().isoformat()
    with _LOCK:
        data = _load()
        totals = data.setdefault("totals", {})
        totals[event] = totals.get(event, 0) + 1

        day = data.setdefault("daily", {}).setdefault(today, {"events": {}, "visitors": {}})
        events = day.setdefault("events", {})
        events[event] = events.get(event, 0) + 1
        if ip:
            vh = visitor_hash(ip)
            visitors = day.setdefault("visitors", {})
            visitors[vh] = visitors.get(vh, 0) + 1
            if event == "page_view":
                all_time = data.setdefault("all_time_visitors", {})
                all_time.setdefault(vh, today)

        recent = data.setdefault("recent", [])
        recent.insert(0, {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
            "path": path,
        })
        del recent[_RECENT_LIMIT:]

        _prune_daily(data["daily"])
        _save(data)


def summary():
    with _LOCK:
        data = _load()

    totals = data.get("totals", {})
    daily = data.get("daily", {})
    rows = []
    for day in sorted(daily, reverse=True)[:14]:
        entry = daily[day]
        events = entry.get("events", {})
        rows.append({
            "date": day,
            "unique_visitors": len(entry.get("visitors", {})),
            "page_views": events.get("page_view", 0),
            "preloads": events.get("preload", 0),
            "searches": events.get("search", 0),
            "searches_done": events.get("search_done", 0),
        })

    today = date.today().isoformat()
    week_cutoff = (date.today() - timedelta(days=6)).isoformat()
    week_visitors = set()
    for day, entry in daily.items():
        if day >= week_cutoff:
            week_visitors.update(entry.get("visitors", {}))

    today_visitors = len(daily.get(today, {}).get("visitors", {}))
    new_today = sum(
        1 for first_seen in data.get("all_time_visitors", {}).values()
        if first_seen == today
    )

    return {
        "totals": {
            "unique_visitors_all_time": len(data.get("all_time_visitors", {})),
            "unique_visitors_today": today_visitors,
            "unique_visitors_7d": len(week_visitors),
            "new_visitors_today": new_today,
            "page_views": totals.get("page_view", 0),
            "preloads": totals.get("preload", 0),
            "searches": totals.get("search", 0),
            "searches_done": totals.get("search_done", 0),
            "search_errors": totals.get("search_error", 0),
        },
        "daily": rows,
        "recent": data.get("recent", [])[:20],
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
