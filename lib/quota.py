"""How much of the Claude and Antigravity plans is used, plus 5h/7d pace & cross-pool balancing.

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
PACE_SAVER_7D_MIN = float(os.environ.get("CR_PACE_7D_MIN", "25"))
PACE_SAVER_PROJ = float(os.environ.get("CR_PACE_PROJ_AT", "105"))
AGY_SHIFT_AT = float(os.environ.get("CR_AGY_SHIFT_AT", "45"))
AGY_SHIFT_GAP = float(os.environ.get("CR_AGY_SHIFT_GAP", "15"))
MAX_AGE = 6 * 3600
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


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


def window_pace(q=None, now=None):
    """Per-window pace stats: {key: {"used", "elapsed_pct", "ratio", "projected", "resets_at"}}."""
    q = latest() if q is None else q
    now = time.time() if now is None else now
    if not q or now - q.get("updated", now) > MAX_AGE:
        return {}
    out = {}
    for key, span in WINDOW_SECONDS.items():
        w = q.get(key) or {}
        u = w.get("used_percentage")
        resets = w.get("resets_at")
        if not isinstance(u, (int, float)) or (isinstance(resets, (int, float)) and resets <= now):
            continue
        if isinstance(resets, (int, float)):
            rem = max(0.0, min(float(span), resets - now))
            elapsed_pct = max(5.0, min(100.0, 100.0 * (span - rem) / span))
        else:
            elapsed_pct = 50.0
        ratio = u / elapsed_pct if elapsed_pct > 0 else 1.0
        projected = round(u / (elapsed_pct / 100.0), 1)
        out[key] = {
            "used": float(u),
            "elapsed_pct": round(elapsed_pct, 1),
            "ratio": round(ratio, 2),
            "projected": projected,
            "resets_at": resets,
        }
    return out


def pace_status(q=None, now=None):
    """Check if 7-day or 5-hour burn rate will exhaust the Claude window before reset."""
    q = latest() if q is None else q
    wp = window_pace(q, now=now)
    d7 = wp.get("seven_day")
    h5 = wp.get("five_hour")
    if d7 and d7["used"] >= PACE_SAVER_7D_MIN and d7["projected"] >= PACE_SAVER_PROJ:
        return {
            "hot": True,
            "window": "seven_day",
            "used": d7["used"],
            "elapsed_pct": d7["elapsed_pct"],
            "projected": d7["projected"],
            "ratio": d7["ratio"],
            "reason": f"7-day at {d7['used']:.0f}% with {d7['elapsed_pct']:.0f}% of week elapsed (proj {d7['projected']:.0f}%)",
        }
    if h5 and h5["used"] >= 55 and h5["projected"] >= 115:
        return {
            "hot": True,
            "window": "five_hour",
            "used": h5["used"],
            "elapsed_pct": h5["elapsed_pct"],
            "projected": h5["projected"],
            "ratio": h5["ratio"],
            "reason": f"5-hour at {h5['used']:.0f}% with {h5['elapsed_pct']:.0f}% elapsed (proj {h5['projected']:.0f}%)",
        }
    return {
        "hot": False,
        "window": "seven_day" if d7 else ("five_hour" if h5 else None),
        "used": d7["used"] if d7 else (h5["used"] if h5 else None),
        "elapsed_pct": d7["elapsed_pct"] if d7 else (h5["elapsed_pct"] if h5 else None),
        "projected": d7["projected"] if d7 else (h5["projected"] if h5 else None),
        "ratio": d7["ratio"] if d7 else (h5["ratio"] if h5 else None),
        "reason": None,
    }


def pool_balance(claude_q=None, agy_pools=None, now=None):
    """Cross-subscription & cross-pool traffic policy across Claude (5h/7d) and Agy (Claude/Gemini)."""
    claude_q = latest() if claude_q is None else claude_q
    if agy_pools is None:
        try:
            import agy_quota
            agy_pools = agy_quota.latest()
        except Exception:
            agy_pools = []
    pools_map = {p["pool"]: p for p in (agy_pools or []) if isinstance(p, dict) and "pool" in p}
    agy_c = (pools_map.get("Claude") or {}).get("used")
    agy_g = (pools_map.get("Gemini") or {}).get("used")
    pace = pace_status(claude_q, now=now)
    lim_active, lim_pct = saver(claude_q)

    shift_gemini = False
    shift_reason = None
    if isinstance(agy_c, (int, float)) and isinstance(agy_g, (int, float)):
        if (agy_c >= AGY_SHIFT_AT and (agy_c - agy_g) >= AGY_SHIFT_GAP) or (agy_c >= 75 and agy_g < agy_c):
            shift_gemini = True
            shift_reason = f"Agy Claude pool {agy_c:.0f}% vs Gemini {agy_g:.0f}%"

    return {
        "agy_claude": agy_c,
        "agy_gemini": agy_g,
        "shift_to_gemini": shift_gemini,
        "shift_reason": shift_reason,
        "pace": pace,
        "saver_limit": lim_active,
        "claude_used": lim_pct,
    }
