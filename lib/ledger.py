"""What one cr session did: routes taken, tokens per model and provider, and the saving
against running the same work on Opus alone.

The comparison is deliberately conservative. It prices the tokens that were actually
processed (front desk, subagents and agy) as if Opus had processed all of them. It does not
credit the context that hand-offs kept out of the main conversation, which in practice is
the larger saving. Prices are only weights: see prices.json.

Transcripts are read incrementally and the running totals cached, so the status line can
call this on every refresh.
"""
import calendar
import collections
import glob
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
PRICES = json.loads((HERE / "prices.json").read_text())
FIELDS = ("input", "output", "cache_read", "cache_write")
CACHE_VERSION = 2


def price_of(model):
    fams = PRICES["families"]
    for name, p in fams.items():
        if name in model:
            return p
    return fams[PRICES["baseline"]]


def cost(model, c):
    p = price_of(model)
    return sum(c.get(f, 0) * p[f] for f in FIELDS) / 1e6


def opus_cost(c):
    p = PRICES["families"][PRICES["baseline"]]
    return sum(c.get(f, 0) * p[f] for f in FIELDS) / 1e6


def short(model):
    """claude-sonnet-5-5 -> sonnet, gemini-3.8-flash-medium -> flash-medium."""
    for fam in PRICES["families"]:
        if fam in model:
            return fam
    parts = model.split("-")
    return "-".join(parts[-2:]) if len(parts) > 2 else model


def _usage(u):
    return {"input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0}


def _iso_to_ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


class Ledger:
    def __init__(self, session_id, transcript_path):
        self.sid = session_id
        if not transcript_path:  # older log lines did not record it
            found = glob.glob(str(Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                                  / "projects" / "*" / f"{session_id}.jsonl"))
            transcript_path = found[0] if found else None
        self.transcript = Path(transcript_path) if transcript_path else None
        self.cache_file = CACHE / f"ledger-{session_id}.json"
        self.s = self._load()

    def _load(self):
        try:
            state = json.loads(self.cache_file.read_text())
            if state.get("v") == CACHE_VERSION:
                return state
        except (OSError, ValueError):
            pass
        return {"v": CACHE_VERSION, "offsets": {}, "seen": {}, "main": {}, "agents": {},
                "spawns": {}, "daily": {}}

    def _save(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s))
        os.replace(tmp, self.cache_file)

    def _read_new(self, path):
        """Yield records appended to path since the last call."""
        key = str(path)
        start = self.s["offsets"].get(key, 0)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < start:
            start = 0
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        self.s["offsets"][key] = start + end
        for line in data[:end].decode("utf-8", "ignore").splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue

    def _count(self, path, bucket):
        recent = self.s["seen"].get(str(path), [])  # in order, so trimming keeps the newest
        seen = set(recent)
        for d in self._read_new(path):
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            if d.get("type") == "assistant":
                for block in m.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if block.get("name") in ("Agent", "Task"):
                        self.s["spawns"][block.get("id")] = {
                            "ts": _iso_to_ts(d.get("timestamp")),
                            "agent": inp.get("subagent_type") or "general-purpose",
                            "desc": inp.get("description") or ""}
            u, model = m.get("usage"), m.get("model")
            if not u or not model or model == "<synthetic>":
                continue
            key = f"{m.get('id')}|{d.get('requestId')}"
            if key in seen:
                continue
            seen.add(key)
            recent.append(key)
            c = bucket.setdefault(model, dict.fromkeys(FIELDS, 0))
            day = self.s["daily"].setdefault((d.get("timestamp") or "")[:10] or "unknown", {})
            dc = day.setdefault(f"Claude|{model}", dict.fromkeys(FIELDS, 0))
            for f, v in _usage(u).items():
                c[f] += v
                dc[f] += v
        self.s["seen"][str(path)] = recent[-500:]

    def refresh(self):
        if self.transcript and self.transcript.exists():
            self._count(self.transcript, self.s["main"])
            sub = self.transcript.with_suffix("") / "subagents"
            for path in sorted(glob.glob(str(sub / "agent-*.jsonl"))):
                agent = self.s["agents"].setdefault(path, {"usage": {}})
                if "tool_use_id" not in agent:
                    try:
                        meta = json.loads(Path(path[:-6] + ".meta.json").read_text())
                        agent["tool_use_id"] = meta.get("toolUseId")
                        agent["type"] = meta.get("agentType")
                    except (OSError, ValueError):
                        pass
                self._count(Path(path), agent["usage"])
        self._save()
        return self

    def agy_runs(self):
        out = []
        try:
            with open(STATE / "agy.jsonl") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("session") == self.sid:
                        out.append(r)
        except OSError:
            pass
        return out

    def prompts(self):
        out = []
        try:
            with open(STATE / "routes.jsonl") as f:
                for line in f:
                    if self.sid not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("session") == self.sid:
                        out.append(r)
        except OSError:
            pass
        return out

    def summary(self):
        """Totals, the saving against all-Opus, and a time-ordered feed of events."""
        by = collections.defaultdict(lambda: dict.fromkeys(FIELDS, 0))  # (provider, model)
        actual = baseline = 0.0
        feed = []

        for model, c in self.s["main"].items():
            for f in FIELDS:
                by[("Claude", model)][f] += c[f]
            actual += cost(model, c)
            baseline += opus_cost(c)

        for agent in self.s["agents"].values():
            spawn = self.s["spawns"].get(agent.get("tool_use_id")) or {}
            a_cost = a_opus = 0.0
            tokens, models = 0, []
            for model, c in agent["usage"].items():
                for f in FIELDS:
                    by[("Claude", model)][f] += c[f]
                a_cost += cost(model, c)
                a_opus += opus_cost(c)
                tokens += sum(c.values())
                models.append(short(model))
            actual += a_cost
            baseline += a_opus
            feed.append({"ts": spawn.get("ts", 0), "kind": "route",
                         "route": spawn.get("agent") or agent.get("type") or "subagent",
                         "provider": "Claude", "model": "/".join(models) or "?",
                         "desc": spawn.get("desc", ""), "tokens": tokens,
                         "cost": a_cost, "opus": a_opus})

        for r in self.agy_runs():
            c = {"input": r.get("input", 0), "cache_read": r.get("cached", 0),
                 "output": r.get("output", 0) + r.get("thinking", 0), "cache_write": 0}
            for f in FIELDS:
                by[("Antigravity", r.get("model", "?"))][f] += c[f]
            a_opus = opus_cost(c)
            baseline += a_opus  # costs nothing on the Claude plan
            feed.append({"ts": r.get("ts", 0), "kind": "route", "route": "agy",
                         "provider": "Antigravity", "model": r.get("model", "?"),
                         "desc": r.get("status", ""), "tokens": sum(c.values()),
                         "cost": 0.0, "opus": a_opus})

        for p in self.prompts():
            feed.append({"ts": p.get("ts", 0), "kind": "prompt", "route": p.get("route"),
                         "desc": p.get("prompt", ""), "context": p.get("context", 0)})

        feed.sort(key=lambda e: e["ts"])
        last = next((e for e in reversed(feed) if e["kind"] == "route"), None)
        return {
            "by": {f"{prov}|{model}": c for (prov, model), c in by.items()},
            "actual": actual, "baseline": baseline,
            "saved": baseline - actual,
            "saved_pct": 100 * (baseline - actual) / baseline if baseline else 0.0,
            "feed": feed, "last": last,
        }


def latest_session():
    """The cr session that most recently received a prompt: (session_id, transcript_path)."""
    try:
        with open(STATE / "routes.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("session"):
            return r["session"], r.get("transcript")
    return None, None
