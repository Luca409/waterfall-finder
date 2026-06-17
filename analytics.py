import hashlib
import json
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ANALYTICS_PATH = Path(os.environ.get("ANALYTICS_PATH", "data/cache/analytics.json"))
_STATS_SALT = os.environ.get("STATS_SALT", "waterfall-finder")
_LOCK = threading.Lock()
_RECENT_LIMIT = 50
_DAILY_RETENTION_DAYS = 90

_BOT_UA_RE = re.compile(
    r"bot|crawler|spider|scraper|curl|wget|python-requests|python/|httpx|aiohttp|"
    r"go-http-client|java/|libwww|headless|slurp|mediapartners|semrush|ahrefs|"
    r"petalbot|dotbot|bytespider|gptbot|claudebot|anthropic|facebookexternalhit|"
    r"bingpreview|yandex|baiduspider|duckduckbot|applebot|twitterbot|linkedinbot|"
    r"discordbot|telegrambot|whatsapp|preview|fetcher|httpclient|okhttp|"
    r"postman|insomnia|scrapy|phantomjs|selenium|puppeteer|playwright",
    re.I,
)


def is_bot(user_agent):
    if not user_agent or not user_agent.strip():
        return True
    return bool(_BOT_UA_RE.search(user_agent))


def visitor_hash(ip):
    return hashlib.sha256(f"{_STATS_SALT}:{ip}".encode()).hexdigest()[:12]


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


def _kind_for(user_agent, ip):
    if user_agent is not None:
        return "bot" if is_bot(user_agent) else "human"
    if ip:
        return "human"
    return None


def _bump_event(bucket, event):
    bucket[event] = bucket.get(event, 0) + 1


def _has_kind_split(entry):
    return bool(entry.get("events_human") or entry.get("events_bot"))


def _daily_event(entry, event, kind):
    if kind == "human":
        if _has_kind_split(entry):
            return entry.get("events_human", {}).get(event, 0)
        return entry.get("events", {}).get(event, 0)
    if _has_kind_split(entry):
        return entry.get("events_bot", {}).get(event, 0)
    return 0


def _totals_event(data, event, kind):
    if kind == "human":
        if data.get("totals_human") or data.get("totals_bot"):
            return data.get("totals_human", {}).get(event, 0)
        return data.get("totals", {}).get(event, 0)
    if data.get("totals_human") or data.get("totals_bot"):
        return data.get("totals_bot", {}).get(event, 0)
    return 0


def record(event, path="", ip="", user_agent=None):
    today = date.today().isoformat()
    kind = _kind_for(user_agent, ip)
    with _LOCK:
        data = _load()
        totals = data.setdefault("totals", {})
        _bump_event(totals, event)

        day = data.setdefault("daily", {}).setdefault(today, {"events": {}})
        events = day.setdefault("events", {})
        _bump_event(events, event)

        if kind:
            kind_totals = data.setdefault(f"totals_{kind}", {})
            _bump_event(kind_totals, event)
            kind_events = day.setdefault(f"events_{kind}", {})
            _bump_event(kind_events, event)
        elif event in ("search_done", "search_error"):
            human_totals = data.setdefault("totals_human", {})
            _bump_event(human_totals, event)
            human_events = day.setdefault("events_human", {})
            _bump_event(human_events, event)

        if ip and kind:
            vh = visitor_hash(ip)
            visitors = day.setdefault(f"visitors_{kind}", {})
            visitors[vh] = visitors.get(vh, 0) + 1
            if event == "page_view":
                all_time = data.setdefault(f"all_time_{kind}_visitors", {})
                all_time.setdefault(vh, today)

        recent = data.setdefault("recent", [])
        recent.insert(0, {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
            "path": path,
            "kind": kind or "human",
        })
        del recent[_RECENT_LIMIT:]

        _prune_daily(data["daily"])
        _save(data)


def _count_visitors(daily, day_key, kind):
    entry = daily.get(day_key, {})
    count = len(entry.get(f"visitors_{kind}", {}))
    if kind == "human":
        count += len(entry.get("visitors", {}))
    return count


def _count_all_time_visitors(data, kind):
    count = len(data.get(f"all_time_{kind}_visitors", {}))
    if kind == "human":
        count += len(data.get("all_time_visitors", {}))
    return count


def summary():
    with _LOCK:
        data = _load()

    totals = data.get("totals", {})
    daily = data.get("daily", {})
    today = date.today().isoformat()
    week_cutoff = (date.today() - timedelta(days=6)).isoformat()

    human_week = set()
    bot_week = set()
    for day, entry in daily.items():
        if day >= week_cutoff:
            human_week.update(entry.get("visitors_human", {}))
            human_week.update(entry.get("visitors", {}))
            bot_week.update(entry.get("visitors_bot", {}))

    new_human_today = sum(
        1 for first_seen in data.get("all_time_human_visitors", {}).values()
        if first_seen == today
    )
    new_human_today += sum(
        1 for first_seen in data.get("all_time_visitors", {}).values()
        if first_seen == today
    )

    rows = []
    for day in sorted(daily, reverse=True)[:14]:
        entry = daily[day]
        rows.append({
            "date": day,
            "human_visitors": _count_visitors(daily, day, "human"),
            "bot_visitors": _count_visitors(daily, day, "bot"),
            "human_page_views": _daily_event(entry, "page_view", "human"),
            "bot_page_views": _daily_event(entry, "page_view", "bot"),
            "human_preloads": _daily_event(entry, "preload", "human"),
            "bot_preloads": _daily_event(entry, "preload", "bot"),
            "human_searches": _daily_event(entry, "search", "human"),
            "searches_done": _daily_event(entry, "search_done", "human"),
        })

    human_totals = {
        "human_visitors_all_time": _count_all_time_visitors(data, "human"),
        "human_visitors_today": _count_visitors(daily, today, "human"),
        "human_visitors_7d": len(human_week),
        "new_human_visitors_today": new_human_today,
        "bot_visitors_all_time": _count_all_time_visitors(data, "bot"),
        "bot_visitors_today": _count_visitors(daily, today, "bot"),
        "bot_visitors_7d": len(bot_week),
        "human_page_views": _totals_event(data, "page_view", "human"),
        "bot_page_views": _totals_event(data, "page_view", "bot"),
        "human_preloads": _totals_event(data, "preload", "human"),
        "bot_preloads": _totals_event(data, "preload", "bot"),
        "human_searches": _totals_event(data, "search", "human"),
        "searches_done": totals.get("search_done", 0),
        "search_errors": totals.get("search_error", 0),
    }

    return {
        "totals": {
            **human_totals,
            # legacy keys for tests / backward compat
            "unique_visitors_all_time": human_totals["human_visitors_all_time"],
            "unique_visitors_today": human_totals["human_visitors_today"],
            "unique_visitors_7d": human_totals["human_visitors_7d"],
            "new_visitors_today": human_totals["new_human_visitors_today"],
            "page_views": totals.get("page_view", 0),
            "preloads": totals.get("preload", 0),
            "searches": totals.get("search", 0),
        },
        "daily": rows,
        "recent": data.get("recent", [])[:20],
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
