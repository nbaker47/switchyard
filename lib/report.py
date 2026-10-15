"""Everything switchyard knows, as one JSON document, for dashboards to draw.

    cr report [--days 14] [--json]

The shape is versioned ("schema"); fields are only ever added. Costs are API list prices
used as weights (see prices.json); "baseline" is the same work priced as if Opus had done
all of it, and Antigravity work costs 0 on the Claude plan.
"""
import argparse
import collections
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota  # noqa: E402
from ledger import FIELDS, STATE, Ledger, cost, opus_cost, short  # noqa: E402

SCHEMA = 2  # 2: adds requests


def sessions_since(since):
    found = {}
    try:
        with open(STATE / "routes.jsonl") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sid = r.get("session")
                if not sid or r.get("ts", 0) < since:
                    continue
                s = found.setdefault(sid, {"id": sid, "cwd": r.get("cwd"), "first": r["ts"],
                                           "transcript": None, "prompts": 0})
                s["last"] = r["ts"]
                s["prompts"] += 1
                s["transcript"] = r.get("transcript") or s["transcript"]
    except OSError:
        pass
    return found


def build(days):
    since = time.time() - days * 86400
    first_day = datetime.date.fromtimestamp(since).isoformat()
    daily = collections.defaultdict(lambda: collections.defaultdict(lambda: dict.fromkeys(FIELDS, 0)))
    models = collections.defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    taken, suggested = collections.Counter(), collections.Counter()
    feed, sessions, requests = [], [], []
    tot_actual = tot_base = 0.0

    for sid, meta in sessions_since(since).items():
        led = Ledger(sid, meta["transcript"]).refresh()
        s = led.summary()
        for day, by in led.s.get("daily", {}).items():
            if day >= first_day:
                for key, c in by.items():
                    for f in FIELDS:
                        daily[day][key][f] += c[f]
        for r in led.agy_runs():
            if r.get("ts", 0) >= since:
                day = datetime.date.fromtimestamp(r["ts"]).isoformat()
                c = daily[day][f"Antigravity|{r.get('model', '?')}"]
                c["input"] += r.get("input", 0)
                c["cache_read"] += r.get("cached", 0)
                c["output"] += r.get("output", 0) + r.get("thinking", 0)
        for key, c in s["by"].items():
            for f in FIELDS:
                models[key][f] += c[f]
        for e in s["feed"]:
            if e["kind"] == "route":
                taken[e["route"].split(":")[-1]] += 1
            else:
                suggested[e["route"]] += 1
            feed.append({**e, "session": sid})
        requests += [{**r, "session": sid} for r in s["requests"] if r["ts"] >= since]
        tot_actual += s["actual"]
        tot_base += s["baseline"]
        sessions.append({
            "id": sid, "cwd": meta["cwd"], "first": meta["first"], "last": meta["last"],
            "prompts": meta["prompts"], "actual": round(s["actual"], 4),
            "baseline": round(s["baseline"], 4), "saved_pct": round(s["saved_pct"], 1),
            "tokens": sum(sum(c.values()) for c in s["by"].values()),
        })

    def money(key, c):
        prov, model = key.split("|", 1)
        return (0.0 if prov == "Antigravity" else cost(model, c)), opus_cost(c)

    series = []
    d = datetime.date.fromtimestamp(since)
    while d <= datetime.date.today():
        by = daily.get(d.isoformat(), {})
        row = {"date": d.isoformat(), "actual": 0.0, "baseline": 0.0,
               "tokens": {"Claude": 0, "Antigravity": 0}, "models": {}}
        for key, c in by.items():
            a, b = money(key, c)
            row["actual"] += a
            row["baseline"] += b
            row["tokens"][key.split("|")[0]] += sum(c.values())
            row["models"][key] = sum(c.values())
        row["actual"], row["baseline"] = round(row["actual"], 4), round(row["baseline"], 4)
        series.append(row)
        d += datetime.timedelta(days=1)

    q = quota.latest()
    active, pct = quota.saver(q)
    feed.sort(key=lambda e: e["ts"])
    return {
        "schema": SCHEMA, "generated": time.time(), "days": days,
        "quota": q, "saver": {"active": active, "used": pct, "threshold": quota.SAVER_AT},
        "totals": {
            "actual": round(tot_actual, 4), "baseline": round(tot_base, 4),
            "saved": round(tot_base - tot_actual, 4),
            "saved_pct": round(100 * (tot_base - tot_actual) / tot_base, 1) if tot_base else 0.0,
            "tokens": {p: sum(r["tokens"][p] for r in series) for p in ("Claude", "Antigravity")},
            "sessions": len(sessions),
        },
        "daily": series,
        "models": sorted(({"provider": k.split("|")[0], "model": k.split("|", 1)[1],
                           "short": short(k.split("|", 1)[1]), "tokens": sum(c.values()),
                           "actual": round(money(k, c)[0], 4), "baseline": round(money(k, c)[1], 4)}
                          for k, c in models.items()), key=lambda m: -m["tokens"]),
        "routes": {"taken": dict(taken), "suggested": dict(suggested)},
        "sessions": sorted(sessions, key=lambda s: -s["last"]),
        "feed": feed[-60:],
        "requests": sorted(requests, key=lambda r: r["ts"])[-200:],
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr report", description=__doc__.split("\n")[0])
    p.add_argument("--days", type=float, default=14)
    p.add_argument("--json", action="store_true", help="machine-readable (the default output)")
    a = p.parse_args(argv)
    json.dump(build(a.days), sys.stdout, indent=None if a.json else 2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
