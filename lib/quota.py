"""How much of the Claude plan is used, as Claude Code last reported it.

Claude Code hands the status line the plan's 5-hour and 7-day usage; switchyard's status
line saves that per session. The limits are per account, so the newest file is current
for every session.
"""
import json
import os
import time
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
SAVER_AT = float(os.environ.get("CR_SAVER_AT", "80"))
MAX_AGE = 6 * 3600


def latest():
    """{"five_hour": {...}, "seven_day": {...}, "updated": ts}, or {} if nothing recent."""
    files = sorted(CACHE.glob("status-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:5]:
        try:
            limits = json.loads(f.read_text()).get("rate_limits") or {}
        except (OSError, ValueError):
            continue
        if limits:
            return {**limits, "updated": f.stat().st_mtime}
    return {}


def used(q=None):
    """The higher of the 5-hour and 7-day usage in percent, ignoring windows that reset."""
    q = latest() if q is None else q
    if not q or time.time() - q.get("updated", 0) > MAX_AGE:
        return None
    vals = []
    for key in ("five_hour", "seven_day"):
        w = q.get(key) or {}
        if isinstance(w.get("used_percentage"), (int, float)) and w.get("resets_at", 1e18) > time.time():
            vals.append(w["used_percentage"])
    return max(vals) if vals else None


def window(q=None):
    """Which window is the fuller one: "five_hour" or "seven_day" (None if unknown)."""
    q = latest() if q is None else q
    live = {k: (q.get(k) or {}).get("used_percentage") for k in ("five_hour", "seven_day")
            if (q.get(k) or {}).get("resets_at", 1e18) > time.time()}
    live = {k: v for k, v in live.items() if isinstance(v, (int, float))}
    return max(live, key=live.get) if live else None


def saver(q=None):
    """(active, percent): saver mode is on when the plan is at or above CR_SAVER_AT."""
    pct = used(q)
    return (pct is not None and pct >= SAVER_AT), pct
