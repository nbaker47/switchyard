"""Tokens per subscription and model, and the routes taken, as terminal bar charts.

    cr usage [--days N] [--all]

Claude Code numbers come from its own session transcripts (~/.claude/projects): by default
only sessions started with `cr`, or every session with --all. Antigravity numbers come
from the log `cr agy` writes. These are token counts, not quota: neither CLI says how much
of a plan's limit is left.
"""
import argparse
import collections
import glob
import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
WIDTH = 28


def read_jsonl(path, since):
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since:
                    yield r
    except OSError:
        return


def claude_usage(since, sessions):
    """Token totals per model from Claude Code transcripts, main loop and subagents."""
    totals = collections.defaultdict(collections.Counter)
    seen = set()
    # Transcripts carry ISO UTC timestamps, which compare correctly as strings.
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
    for path in glob.glob(str(PROJECTS / "**" / "*.jsonl"), recursive=True):
        if os.path.getmtime(path) < since:
            continue
        p = Path(path)
        sid = p.parent.parent.name if p.parent.name == "subagents" else p.stem
        if sessions is not None and sid not in sessions:
            continue
        with open(path, errors="ignore") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    m = d["message"]
                    u, model = m["usage"], m["model"]
                except (ValueError, KeyError, TypeError):
                    continue
                key = (m.get("id"), d.get("requestId"))
                if model == "<synthetic>" or key in seen or d.get("timestamp", "") < since_iso:
                    continue
                seen.add(key)
                t = totals[model]
                t["calls"] += 1
                t["output"] += u.get("output_tokens") or 0
                t["read"] += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
    return totals


def agy_usage(since):
    totals = collections.defaultdict(collections.Counter)
    for r in read_jsonl(STATE / "agy.jsonl", since):
        t = totals[r.get("model", "?")]
        t["calls"] += 1
        t["output"] += r.get("output", 0) + r.get("thinking", 0)
        t["read"] += r.get("input", 0) + r.get("cached", 0)
        t["failed"] += r.get("status") != "SUCCESS"
    return totals


def fmt(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def bars(title, rows, value, label):
    print(f"\n{title}")
    if not rows:
        print("  (nothing yet)")
        return
    top = max(value(r) for r in rows.values()) or 1
    for name, r in sorted(rows.items(), key=lambda kv: -value(kv[1])):
        n = round(WIDTH * value(r) / top)
        print(f"  {name:<28} {'█' * n}{'░' * (WIDTH - n)} {label(r)}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr usage", description=__doc__.split("\n")[0])
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--all", action="store_true", help="all Claude Code sessions, not only cr ones")
    a = p.parse_args(argv)
    since = time.time() - a.days * 86400

    routes = list(read_jsonl(STATE / "routes.jsonl", since))
    sessions = None if a.all else {r["session"] for r in routes if r.get("session")}
    scope = "all sessions" if a.all else f"{len(sessions)} cr sessions"
    print(f"switchyard usage, last {a.days:g} days ({scope})")

    tokens = lambda r: r["read"] + r["output"]
    label = lambda r: f"{fmt(tokens(r)):>7} tok  {r['calls']:>5} calls  out {fmt(r['output'])}" \
        + (f"  {r['failed']} failed" if r.get("failed") else "")
    claude = claude_usage(since, sessions)
    agy = agy_usage(since)
    bars("Claude Code subscription", claude, tokens, label)
    bars("Antigravity subscription (via cr agy)", agy, tokens, label)

    c, g = sum(tokens(r) for r in claude.values()), sum(tokens(r) for r in agy.values())
    if c + g:
        n = round(WIDTH * c / (c + g))
        print(f"\nSplit  Claude {'█' * n}{'░' * (WIDTH - n)} Antigravity   "
              f"{100 * c / (c + g):.0f}% / {100 * g / (c + g):.0f}%")

    counts = collections.Counter(r["route"] for r in routes)
    bars("Suggested routes", {k: {"n": v} for k, v in counts.items()}, lambda r: r["n"],
         lambda r: f"{r['n']} prompts")
    big = [r["context"] for r in routes if r.get("context")]
    if big:
        print(f"\nContext at each prompt: median {fmt(sorted(big)[len(big) // 2])} tokens, "
              f"max {fmt(max(big))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
