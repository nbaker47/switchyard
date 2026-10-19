"""Antigravity quota per model pool: Claude (with GPT-OSS) and Gemini (Argon and every Flash).

agy exposes no quota itself. It talks to Google's daily Cloud Code endpoint, and that endpoint
reports quota per pool: all Claude models share one, all Gemini models (Argon included, which
has no entry of its own) share another. The `antigravity-usage` CLI reads the production
endpoint, which lags behind, so we run it only to keep its login fresh and fall back to its
numbers, and ask the daily endpoint directly with the same login. The call takes a few
seconds, so the result is cached on disk and refreshed in the background; the dash never
waits for it.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard" / "agy-quota.json"
TOKENS = Path.home() / "Library/Application Support/antigravity-usage/accounts"
DAILY = "https://daily-cloudcode-pa.googleapis.com"
META = {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
TTL = 60
MAX_AGE = 6 * 3600
_busy = threading.Lock()


def pool(model_id):
    """Pool a model id belongs to, or None for ones without a bar (autocomplete, tab, chat ids)."""
    m = model_id.lower()
    if m.startswith(("claude-", "gpt-oss")):
        return "Claude"
    if m.startswith("gemini"):
        return "Gemini"
    return None


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def parse(data):
    """[{"pool", "used", "resets_at"}], used in percent, the fullest model in each pool winning.

    Takes either the CLI's {"models": [{modelId, remainingPercentage, resetTime}]} or the API's
    {"models": {id: {quotaInfo: {remainingFraction, resetTime}}}}.
    """
    models = (data or {}).get("models") or []
    if isinstance(models, dict):
        models = [{"modelId": k, "remainingPercentage": (v.get("quotaInfo") or {}).get("remainingFraction"),
                   "resetTime": (v.get("quotaInfo") or {}).get("resetTime")} for k, v in models.items()]
    pools = {}
    for m in models:
        name = pool(m.get("modelId", ""))
        rem = m.get("remainingPercentage")
        if not name or m.get("isAutocompleteOnly") or not isinstance(rem, (int, float)):
            continue
        used = 100.0 * (1.0 - rem)
        if name not in pools or used > pools[name]["used"]:
            pools[name] = {"pool": name, "used": used, "resets_at": _ts(m.get("resetTime"))}
    return sorted(pools.values(), key=lambda p: ["Claude", "Gemini"].index(p["pool"]))


def _post(path, body, token):
    req = Request(DAILY + path, json.dumps(body).encode(),
                  {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "antigravity"})
    return json.load(urlopen(req, timeout=15))


def _daily():
    """Quota from the daily endpoint using the usage CLI's login, or None."""
    for f in sorted(TOKENS.glob("*/tokens.json")):
        try:
            token = json.loads(f.read_text())["accessToken"]
            proj = _post("/v1internal:loadCodeAssist", {"metadata": META}, token).get("cloudaicompanionProject")
            return parse(_post("/v1internal:fetchAvailableModels", {"project": proj} if proj else {}, token))
        except Exception:  # missing file, bad token, network or HTTP error: try the next account
            continue
    return None


def _refresh():
    try:
        exe = shutil.which("antigravity-usage")
        if not exe:
            return
        # Running the CLI refreshes its login; its own numbers are the fallback.
        out = subprocess.run([exe, "--json"], capture_output=True, text=True, timeout=30).stdout
        pools = _daily() or parse(json.loads(out))
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
