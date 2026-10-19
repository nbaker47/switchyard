"""Antigravity quota per model pool, from the `antigravity-usage` CLI.

agy itself exposes no quota, but `antigravity-usage --json` reads it from Google. Models share
pools (every Claude model draws on one, Gemini Pro and Flash on others), so we show one bar
per pool using the lowest remaining share in it. The CLI takes a few seconds, so the result
is cached on disk and refreshed in the background; the dash never waits for it.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard" / "agy-quota.json"
TTL = 60
MAX_AGE = 6 * 3600
_busy = threading.Lock()


def pool(model_id):
    """Pool a model id belongs to, or None for ones not worth a bar (autocomplete-only etc.)."""
    m = model_id.lower()
    if m.startswith("claude-"):
        return "Claude"
    if m.startswith("gemini") and "pro" in m:
        return "Gemini Pro"
    if m.startswith("gemini") and "flash" in m:
        return "Gemini Flash"
    return None


def parse(data):
    """[{"pool", "used", "resets_at"}] with used in percent; the fullest model in each pool wins."""
    pools = {}
    for m in (data or {}).get("models") or []:
        name = pool(m.get("modelId", ""))
        rem = m.get("remainingPercentage")
        if not name or m.get("isAutocompleteOnly") or not isinstance(rem, (int, float)):
            continue
        try:
            reset = datetime.fromisoformat(m["resetTime"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            reset = None
        used = 100.0 * (1.0 - rem)
        if name not in pools or used > pools[name]["used"]:
            pools[name] = {"pool": name, "used": used, "resets_at": reset}
    order = ["Claude", "Gemini Pro", "Gemini Flash"]
    return sorted(pools.values(), key=lambda p: order.index(p["pool"]))


def _refresh():
    try:
        exe = shutil.which("antigravity-usage")
        if not exe:
            return
        out = subprocess.run([exe, "--json"], capture_output=True, text=True, timeout=30).stdout
        pools = parse(json.loads(out))
        if pools:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps({"ts": time.time(), "pools": pools}))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        _busy.release()


def latest():
    """Cached pools, [] if none yet. Starts a background refresh when the cache is stale."""
    ts, pools = 0, []
    try:
        d = json.loads(CACHE.read_text())
        ts, pools = d["ts"], d["pools"]
    except (OSError, ValueError, KeyError):
        pass
    if time.time() - ts > TTL and _busy.acquire(blocking=False):
        threading.Thread(target=_refresh, daemon=True).start()
    if time.time() - ts > MAX_AGE:
        return []
    return [p for p in pools if p.get("resets_at") is None or p["resets_at"] > time.time()]
