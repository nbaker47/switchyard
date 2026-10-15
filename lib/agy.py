"""Hand a task to the Antigravity CLI (`agy`) and print only its answer.

It runs your own, already signed-in `agy` in print mode. Your Antigravity login is never
read or forwarded. Models are never hard-coded: a tier is matched against the list that
`agy models` returns for your account, so new models are picked up automatically.

    cr agy [--tier fast|cheap|strong|opus|sonnet|<model id>] [--effort LEVEL] "task"
    cr models
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
MODELS_TTL = 6 * 3600

# A tier names a model family by keyword and the compute level to prefer within it.
# Keywords, not ids, so "gemini-4-argon-high" and its successors match without edits.
TIERS = {
    "cheap": (["flash", "lite", "mini"], ["low", "medium", "high"]),
    "fast": (["flash", "lite", "mini"], ["medium", "low", "high"]),
    "strong": (["argon", "pro", "ultra"], ["high", "medium", "low"]),
    "opus": (["opus"], ["medium", "high", "low"]),
    "sonnet": (["sonnet"], ["medium", "high", "low"]),
}


def list_models(refresh=False):
    """Model ids available to this agy login, cached for a few hours."""
    cache = CACHE / "agy-models.txt"
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < MODELS_TTL:
        return cache.read_text().split()
    out = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=60).stdout
    ids = [line.split()[0] for line in out.splitlines()
           if line.strip() and not line.startswith("Fetching") and re.match(r"^[\w.\-]+\s", line + " ")]
    if ids:
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(ids) + "\n")
    return ids


def pick(tier, models):
    """Resolve a tier (or an exact id) against the live model list."""
    if tier in models:
        return tier
    if tier not in TIERS:
        raise SystemExit(f"unknown tier or model: {tier}. Try `cr models`.")
    keywords, levels = TIERS[tier]
    # Whole name parts only: "mini" must not match "gemini".
    family = [m for m in models if set(re.split(r"[-_.]", m)) & set(keywords)]
    if not family:
        raise SystemExit(f"no model on your agy account fits tier '{tier}'. Try `cr models`.")
    for level in levels:
        for m in family:
            if m.endswith("-" + level):
                return m
    return family[0]


def log(record):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "agy.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def run(task, model, effort=None, timeout=0):
    cmd = ["agy", "--model", model, "--output-format", "json"]
    if os.environ.get("CR_YOLO", "1") == "1":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--mode", "accept-edits"]
    if effort:
        cmd += ["--effort", effort]
    if timeout:
        cmd += ["--print-timeout", f"{timeout}s"]
    cmd += ["-p", task]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {"status": "ERROR", "response": (proc.stdout + proc.stderr).strip()}
    usage = result.get("usage") or {}
    log({
        "ts": time.time(), "model": model, "status": result.get("status"),
        "seconds": round(time.time() - started, 1), "cwd": os.getcwd(),
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "conversation": result.get("conversation_id"),
        "task": " ".join(task.split())[:120],
        "input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
        "thinking": usage.get("thinking_tokens", 0), "cached": usage.get("cache_read_tokens", 0),
    })
    return result


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr agy", description=__doc__.split("\n")[0])
    p.add_argument("task", nargs="*")
    p.add_argument("--tier", default="fast")
    p.add_argument("--effort")
    p.add_argument("--timeout", type=int, default=0, help="seconds; 0 waits for the turn")
    p.add_argument("--list", action="store_true", help="list models and how tiers resolve")
    p.add_argument("--refresh", action="store_true", help="re-read the model list")
    a = p.parse_args(argv)

    models = list_models(a.refresh)
    if a.list:
        for m in models:
            print(m)
        print()
        for t in TIERS:
            try:
                print(f"{t:>7} -> {pick(t, models)}")
            except SystemExit:
                print(f"{t:>7} -> (none)")
        return 0
    task = " ".join(a.task) or sys.stdin.read()
    if not task.strip():
        p.error("no task given")
    model = pick(a.tier, models)
    result = run(task, model, a.effort, a.timeout)
    print(f"[agy {model} · {result.get('status', '?')}]")
    print((result.get("response") or "").strip())
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
