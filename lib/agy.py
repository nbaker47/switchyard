"""Hand a task to the Antigravity CLI (`agy`) and print only its answer.

It runs your own, already signed-in `agy` in print mode. Your Antigravity login is never
read or forwarded. Models are never hard-coded: a tier is matched against the list that
`agy models` returns for your account, so new models are picked up automatically.

Warm standby sessions: when you go back and forth on a brainstorm or multi-step task,
`cr agy` keeps the Antigravity `conversation_id` on standby and resumes it (`--conversation`)
without re-sending the CLAUDE.md preamble, then automatically closes it after 3 idle turns
(`CR_STANDBY_TURNS=3`). Pass `--new` (or `CR_AGY_NO_STANDBY=1`) to force a fresh session.

    cr agy [--tier auto|fast|cheap|strong|opus|sonnet|<model id>] [--new] [--effort LEVEL] [--bare] "task"
    cr agy --show-context [--compress]
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compress  # noqa: E402
import flow  # noqa: E402
import quota  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
MODELS_TTL = 6 * 3600
STANDBY_TURNS = int(os.environ.get("CR_STANDBY_TURNS", "3"))
STANDBY_TTL = int(os.environ.get("CR_STANDBY_TTL", "1800"))
STANDBY_COMPACT_AT = int(os.environ.get("CR_STANDBY_COMPACT_AT", "100000"))

# A tier names a model family by keyword and the compute level to prefer within it.
# Keywords, not ids, so "gemini-4-argon-high" and its successors match without edits.
TIERS = {
    "cheap": (["flash", "lite", "mini"], ["low", "medium", "high"]),
    "fast": (["flash", "lite", "mini"], ["medium", "low", "high"]),
    "strong": (["argon", "pro", "ultra"], ["high", "medium", "low"]),
    "opus": (["opus"], ["medium", "high", "low"]),
    "sonnet": (["sonnet"], ["medium", "high", "low"]),
}

DEEP_KEYWORDS = {
    "architect", "architecture", "design", "brainstorm", "strategy", "security",
    "race", "deadlock", "concurrency", "root-cause", "root", "stubborn", "complex",
    "plan", "trade-off", "tradeoff", "redesign", "audit",
    "idea", "ideas", "ideate", "ideation", "concept", "concepts", "pitch", "creative",
}
CODE_KEYWORDS = {
    "fix", "bug", "implement", "refactor", "test", "tests", "failing", "edit",
    "update", "patch", "add", "change", "build", "error", "crash", "feature",
}


# ---- Warm Standby Session Manager (auto-expires after N idle turns + autocompacts at 100k) ----

def _standby_file():
    return CACHE / "standby.json"


def _standby_scope(session_id=None, cwd=None):
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    return f"session:{sid}" if sid else f"cwd:{Path(cwd or os.getcwd()).resolve()}"


def _load_standby():
    try:
        return json.loads(_standby_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_standby_map(data):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = _standby_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _standby_file())
    except OSError:
        pass


def standby_tick(session_id=None, cwd=None, max_idle=None, now=None):
    """Called on each UserPromptSubmit: increments idle_turns by 1 for warm sessions in this
    scope and closes any session that has gone unused for `max_idle` turns (default 3) or TTL.
    Returns `(active_list, closed_list)`.
    """
    max_idle = STANDBY_TURNS if max_idle is None else max_idle
    now = time.time() if now is None else now
    scope = _standby_scope(session_id, cwd)
    data = _load_standby()
    bucket = data.get(scope) or {}
    kept, closed = {}, []
    for model, entry in bucket.items():
        idle = entry.get("idle_turns", 0) + 1
        age = now - entry.get("ts", 0)
        if idle > max_idle or age > STANDBY_TTL:
            closed.append({**entry, "model": model, "reason": f"idle {idle - 1} turns"})
        else:
            kept[model] = {**entry, "idle_turns": idle, "max_idle": max_idle}
    if kept:
        data[scope] = kept
    else:
        data.pop(scope, None)
    _save_standby_map(data)
    return list(kept.values()), closed


def standby_get(model, session_id=None, cwd=None, max_idle=None, compact_at=None, now=None):
    """Return warm standby entry for `model` in current scope if still within idle limit.
    Sets `entry['autocompact'] = True` when `context_tokens >= compact_at` (default 100k).
    """
    max_idle = STANDBY_TURNS if max_idle is None else max_idle
    compact_at = STANDBY_COMPACT_AT if compact_at is None else compact_at
    now = time.time() if now is None else now
    scope = _standby_scope(session_id, cwd)
    bucket = (_load_standby().get(scope)) or {}
    entry = bucket.get(model)
    if not entry:
        return None
    if entry.get("idle_turns", 0) > max_idle or (now - entry.get("ts", 0)) > STANDBY_TTL:
        return None
    ctx_tok = entry.get("context_tokens", 0) or 0
    return {
        **entry,
        "max_idle": max_idle,
        "autocompact": bool(compact_at and ctx_tok >= compact_at),
    }


def standby_record(model, conversation_id, session_id=None, cwd=None, now=None,
                   context_tokens=0, response_text="", provider="Antigravity"):
    """Save or refresh a warm standby session (for either Antigravity or Claude Code subagents),
    resetting `idle_turns` to 0 and storing `context_tokens` + a compact digest for 100k autocompact.
    """
    if not conversation_id:
        return None
    now = time.time() if now is None else now
    scope = _standby_scope(session_id, cwd)
    data = _load_standby()
    bucket = data.setdefault(scope, {})
    prev = bucket.get(model) or {}
    turns = (prev.get("turns", 0) + 1) if prev.get("conversation_id") == conversation_id else 1
    digest = prev.get("digest", "")
    if response_text:
        comp = compress.compress_markdown(response_text)
        digest = " ".join(comp.split())[:600]
    entry = {
        "model": model,
        "provider": provider,
        "conversation_id": conversation_id,
        "ts": now,
        "idle_turns": 0,
        "turns": turns,
        "max_idle": STANDBY_TURNS,
        "context_tokens": int(context_tokens or prev.get("context_tokens", 0) or 0),
        "digest": digest,
    }
    bucket[model] = entry
    _save_standby_map(data)
    return entry


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


def classify_task(task):
    """Classify a task brief into ('cheap'|'fast'|'sonnet'|'opus'|'strong', reason) for --tier auto."""
    words = {w for w in re.findall(r"[a-z0-9_-]{3,}", (task or "").lower())}
    if words & DEEP_KEYWORDS:
        if words & {"brainstorm", "idea", "ideas", "ideate", "ideation", "concept", "concepts", "pitch", "creative", "strategy", "research", "compare", "repo", "whole"}:
            return "strong", "open-ended ideation / creative brainstorm"
        return "opus", "deep technical judgement"
    if words & CODE_KEYWORDS:
        if words & {"bulk", "mechanical", "rename", "all", "format", "migration", "scaffold"}:
            return "fast", "auto-triage: bulk/mechanical code change → fast"
        return "sonnet", "auto-triage: scoped code change → sonnet"
    if len(task or "") < 80:
        return "cheap", "auto-triage: short lookup/check → cheap"
    return "fast", "auto-triage: reading/summarising → fast"


def resolve(tier, models, task="", pools=None, force_claude=False):
    """Resolve `tier` (including `auto`) with live Antigravity pool balancing.
    Returns `(model_id, effective_tier, remapped_from, reason)`.
    """
    orig_tier = tier
    reason = f"tier {tier}"
    if tier == "auto":
        tier, reason = classify_task(task)
        orig_tier = f"auto({tier})"

    remapped_from = None
    if not force_claude and os.environ.get("CR_AGY_FORCE_CLAUDE") != "1" and tier in ("opus", "sonnet"):
        bal = quota.pool_balance(agy_pools=pools)
        if bal.get("shift_to_gemini"):
            agy_g = bal.get("agy_gemini") or 0
            if tier == "opus":
                new_tier = "strong"
            else:
                new_tier = "strong" if agy_g < 40 else "fast"
            try:
                model = pick(new_tier, models)
                remapped_from = orig_tier
                reason = f"pool shift ({bal['shift_reason']}) · {tier}→{new_tier}"
                return model, new_tier, remapped_from, reason
            except SystemExit:
                pass

    model = pick(tier, models)
    return model, tier, remapped_from, reason


def log(record):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "agy.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


MAX_PROGRESS = 30

CONTEXT_MAX_CHARS = 120000
FRAME = ("Project instructions loaded for you from the Claude Code harness. Follow them as "
         "binding. You cannot run Claude skills as tools; read the listed files with your own tools.")


def _chain(cwd):
    """Directories from the filesystem root down to cwd."""
    cwd = Path(cwd).resolve()
    return list(reversed([cwd] + list(cwd.parents)))


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _imports(path, text, seen, depth=0):
    """Files named by simple `@path` lines (whole line, no spaces), followed to a small depth."""
    found = []
    if depth >= 4:
        return found
    for line in text.splitlines():
        line = line.strip()
        if not re.match(r"^@\S+$", line):
            continue
        target = Path(os.path.expanduser(line[1:]))
        if not target.is_absolute():
            target = Path(path).parent / target
        target = target.resolve()
        if target in seen or not target.is_file():
            continue
        body = _read(target)
        if body is None:
            continue
        seen.add(target)
        found.append((str(target), body))
        found += _imports(target, body, seen, depth + 1)
    return found


def claude_md_files(cwd, home):
    """[(absolute path, text)] outermost to innermost, without duplicates."""
    candidates = [Path(home) / ".claude" / "CLAUDE.md"]
    for d in _chain(cwd):
        candidates += [d / "CLAUDE.md", d / "CLAUDE.local.md"]
    files, seen = [], set()
    for c in candidates:
        if not c.is_file():
            continue
        real = c.resolve()
        if real in seen:
            continue
        text = _read(real)
        if text is None:
            continue
        seen.add(real)
        files.append((str(c.absolute() if c.is_symlink() else real), text))
        files += _imports(real, text, seen)
    return files


def _frontmatter(text):
    """name and description from a leading `---` block; handles folded/indented values."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            key = m.group(1)
            meta[key] = m.group(2).strip()
        elif key and line.strip():
            meta[key] += " " + line.strip()
    for k, v in meta.items():
        meta[k] = " ".join(v.strip("|>-+ ").strip("\"'").split())
    return meta


def skills(cwd, home):
    """([(name, description, SKILL.md path)], {folder: [reference file paths]})."""
    roots, seen = [], set()
    for base in [Path(home)] + _chain(cwd):
        root = base / ".claude" / "skills"
        if root.is_dir() and root.resolve() not in seen:
            seen.add(root.resolve())
            roots.append(root)
    found, refs = [], {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name == "SKILL.md":
                    meta = _frontmatter(_read(path) or "")
                    found.append((meta.get("name") or Path(dirpath).name,
                                  meta.get("description", ""), str(path)))
                elif name.endswith(".md") and not name.startswith("."):
                    refs.setdefault(dirpath, []).append(str(path))
    return found, refs


def memory_index(cwd, home):
    """(path, text) of this project's MEMORY.md index, if Claude Code has written one."""
    base = Path(home) / ".claude" / "projects"
    cwd = str(Path(cwd).resolve())
    for slug in (cwd.replace("/", "-"), re.sub(r"[^A-Za-z0-9]", "-", cwd)):
        path = base / slug / "memory" / "MEMORY.md"
        text = _read(path) if path.is_file() else None
        if text is not None:
            return str(path), text
    return None


def _render(files, replaced, found, refs, memory, note, use_wire=False, omitted_skills=0):
    frame = f"{FRAME}\n{compress.COD_WIRE_RULES}" if use_wire else FRAME
    parts = [frame]
    if files:
        parts.append("## Project instructions (CLAUDE.md files, outermost first)")
        if note:
            parts.append(note)
        for i, (path, text) in enumerate(files):
            if i in replaced:
                parts.append(f"### {path}\n(omitted for size, {len(text)} chars: read this file yourself)")
            else:
                parts.append(f"### {path}\n{text.strip()}")
    if found:
        lines = ["## Skills available (read the file before using one)"]
        for name, desc, path in found:
            lines.append(f"- {name}: {desc[:200] or '(no description)'} [{path}]")
        if omitted_skills:
            lines.append(f"- (+{omitted_skills} other skills omitted for token efficiency; pass --full-context to list all)")
        if refs:
            lines.append("\nReference files (read when a skill points to them):")
            for folder in sorted(refs):
                lines.append(f"{folder}/")
                lines += [f"  {p}" for p in refs[folder]]
        parts.append("\n".join(lines))
    if memory:
        parts.append(f"## Memory index\n### {memory[0]}\n{memory[1].strip()}")
    return "\n\n".join(parts) + "\n"


def build_context(cwd=None, home=None, max_chars=None, task=None, use_compress=False):
    """The preamble for agy, and its stats. Empty text when there is nothing to pass on.
    When `use_compress=True`, applies Caveman/TOON markdown compression and task-based skill pruning.
    """
    cwd = cwd or os.getcwd()
    home = home or str(Path.home())
    if max_chars is None:
        max_chars = int(os.environ.get("CR_AGY_CONTEXT_MAX_CHARS") or CONTEXT_MAX_CHARS)
    files = claude_md_files(cwd, home)
    found, refs = skills(cwd, home)
    total_skills = len(found)
    stats = {"claude_md": len(files), "skills": total_skills, "chars": 0, "replaced": 0}
    if not files and not found:
        return "", stats
    memory = memory_index(cwd, home)
    raw_text = _render(files, set(), found, refs, memory, "")
    raw_chars = len(raw_text)

    omitted_skills = 0
    if use_compress and os.environ.get("CR_AGY_FULL_CONTEXT") != "1":
        files = [(p, compress.compress_markdown(t, task=task or "")) for p, t in files]
        if memory:
            memory = (memory[0], compress.compress_markdown(memory[1]))
        found, refs, omitted_skills = compress.filter_skills(found, refs, task=task or "")

    replaced, note = set(), ""
    text = _render(files, replaced, found, refs, memory, note,
                   use_wire=use_compress, omitted_skills=omitted_skills)
    # Over the cap: swap the largest CLAUDE.md files for a pointer, one at a time.
    order = sorted(range(len(files)), key=lambda i: -len(files[i][1]))
    while len(text) > max_chars and len(replaced) < len(order):
        replaced.add(order[len(replaced)])
        names = ", ".join(files[i][0] for i in sorted(replaced))
        note = (f"Note: context cap of {max_chars} chars exceeded; these files are not inlined, "
                f"read them yourself: {names}")
        text = _render(files, replaced, found, refs, memory, note,
                       use_wire=use_compress, omitted_skills=omitted_skills)
    stats.update(chars=len(text), replaced=len(replaced))
    if use_compress:
        stats.update(raw_chars=raw_chars, saved_chars=max(0, raw_chars - len(text)),
                     omitted_skills=omitted_skills)
    return text, stats


def context_line(stats):
    base = (f"context: {stats['claude_md']} CLAUDE.md, {stats['skills']} skills, "
            f"~{stats['chars'] / 4000:.1f}k tokens (chars/4)"
            + (f", {stats['replaced']} CLAUDE.md pointed to, not inlined" if stats["replaced"] else ""))
    if stats.get("saved_chars", 0) > 0 and stats.get("raw_chars", 0) > 0:
        pct = round(100.0 * stats["saved_chars"] / stats["raw_chars"])
        base += f" [compressed −{pct}%]"
    return base


def bare_requested(flag=False):
    return flag or os.environ.get("CR_AGY_BARE") == "1"


def describe(step):
    """One short line for a tool step: the tool and the first thing it was given."""
    info = step.get("tool_info") or {}
    params = info.get("parameters") or {}
    detail = next((str(v) for v in params.values() if isinstance(v, str) and v.strip()), "")
    if "/" in detail and len(detail) > 50:
        detail = detail.rsplit("/", 1)[-1]
    detail = " ".join(detail.split())
    return f"{step.get('tool_name') or info.get('name') or 'tool'} {detail[:70]}".rstrip()


def run(task, model, effort=None, timeout=0, progress=print, preamble="", reason="",
        remapped_from=None, conversation_id=None, comp_stats=None, standby_info=None):
    """Run agy, reporting each tool step as it happens, and return its final result."""
    cmd = ["agy", "--model", model, "--output-format", "stream-json"]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    if os.environ.get("CR_YOLO", "1") == "1":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--mode", "accept-edits"]
    if effort:
        cmd += ["--effort", effort]
    if timeout:
        cmd += ["--print-timeout", f"{timeout}s"]
    cmd += ["-p", f"{preamble}\n## Task\n{task}" if preamble else task]
    started = time.time()
    result, shown, hidden, other = {}, 0, 0, []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                other.append(line)
                continue
            if event.get("event") == "result":
                result = event.get("result") or {}
            step = event.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE" and progress:
                if shown < MAX_PROGRESS:
                    progress(f"  ├─ {time.time() - started:4.0f}s  {describe(step)}")
                    shown += 1
                else:
                    hidden += 1
    if hidden and progress:
        progress(f"  ├─ … and {hidden} more steps")
    if not result:
        result = {"status": "ERROR", "response": "".join(other).strip() or "agy returned nothing"}
    usage = result.get("usage") or {}
    log({
        "ts": time.time(), "model": model, "status": result.get("status"),
        "seconds": round(time.time() - started, 1), "cwd": os.getcwd(),
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "conversation": result.get("conversation_id") or conversation_id,
        "resumed": bool(conversation_id),
        "task": " ".join(task.split())[:120],
        "reason": reason, "remapped_from": remapped_from,
        "comp_stats": comp_stats, "standby_info": standby_info,
        "input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
        "thinking": usage.get("thinking_tokens", 0), "cached": usage.get("cache_read_tokens", 0),
    })
    return result


BACKGROUND_FLAGS = {"--run_in_background", "--run-in-background", "--background"}


def strip_background(argv):
    """Backgrounding belongs to whoever runs this command (e.g. Claude Code's Bash tool), but
    models sometimes write it as a flag. Drop it, and a true/false value after it, instead of
    failing the hand-off or swallowing the task text."""
    out, dropped, skip_value = [], False, False
    for arg in argv:
        if skip_value and arg.lower() in ("true", "false", "1", "0"):
            skip_value = False
            continue
        skip_value = False
        name = arg.split("=", 1)[0]
        if name in BACKGROUND_FLAGS:
            dropped, skip_value = True, "=" not in arg
            continue
        out.append(arg)
    return out, dropped


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr agy", description=__doc__.split("\n")[0])
    p.add_argument("task", nargs="*")
    p.add_argument("--tier", default="fast")
    p.add_argument("--why", default="", help="short routing justification shown in the flow banner")
    p.add_argument("--effort")
    p.add_argument("--timeout", type=int, default=0, help="seconds; 0 waits for the turn")
    p.add_argument("--list", action="store_true", help="list models and how tiers resolve")
    p.add_argument("--bare", action="store_true",
                   help="no project context preamble (also CR_AGY_BARE=1)")
    p.add_argument("--new", action="store_true",
                   help="start a fresh agy session instead of reusing a warm standby session")
    p.add_argument("--conversation", help="explicitly resume a specific agy conversation ID")
    p.add_argument("--full-context", action="store_true",
                   help="disable Caveman/skill compression on preamble")
    p.add_argument("--force-claude", action="store_true",
                   help="do not remap opus/sonnet to Gemini Argon/Flash when Agy Claude pool is hot")
    p.add_argument("--show-context", action="store_true",
                   help="print the preamble that would be sent, and exit")
    p.add_argument("--compress", action="store_true",
                   help="with --show-context, preview the Caveman-compressed preamble")
    p.add_argument("--refresh", action="store_true", help="re-read the model list")
    argv, dropped = strip_background(sys.argv[1:] if argv is None else argv)
    if dropped:
        print("  (note: backgrounding is set on the tool call that runs cr agy; flag ignored)",
              file=sys.stderr)
    # A hand-off must not die over a stray argument a model added ("---", an unknown flag):
    # drop flag-like leftovers with a note, and keep any other words as part of the task.
    a, extra = p.parse_known_args(argv)
    junk = [x for x in extra if x.startswith("-")]
    a.task += [x for x in extra if not x.startswith("-")]
    if junk:
        print(f"  (note: ignored unknown arguments: {' '.join(junk)})", file=sys.stderr)

    if a.show_context:
        if bare_requested(a.bare):
            print("(bare: no preamble)")
            return 0
        text, stats = build_context(task=" ".join(a.task) or None, use_compress=a.compress)
        print(context_line(stats), file=sys.stderr)
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    models = list_models(a.refresh)
    if a.list:
        for m in models:
            print(m)
        print()
        for t in TIERS:
            try:
                m_id, eff, rem, why = resolve(t, models, force_claude=a.force_claude)
                note = f" (pool-shifted from {rem}: {why})" if rem else ""
                print(f"{t:>7} -> {m_id}{note}")
            except SystemExit:
                print(f"{t:>7} -> (none)")
        return 0
    task = " ".join(a.task) or sys.stdin.read()
    if not task.strip():
        p.error("no task given")
    model, eff_tier, remapped_from, reason = resolve(
        a.tier, models, task=task, force_claude=a.force_claude
    )
    if a.why.strip():
        reason = a.why.strip() if reason.startswith("tier ") else f"{a.why.strip()} · {reason}"
    bal = quota.pool_balance()

    # Check if a warm standby session exists for this model (unless --new or CR_AGY_NO_STANDBY=1).
    standby_enabled = os.environ.get("CR_AGY_NO_STANDBY") != "1"
    sb_entry = None
    conv_id = a.conversation
    if not conv_id and standby_enabled and not a.new:
        sb_entry = standby_get(model)
        if sb_entry:
            if sb_entry.get("autocompact"):
                # Warm session reached 100k token autocompact ceiling -> roll over to fresh session with digest
                old_tok = sb_entry.get("context_tokens", 0)
                digest = sb_entry.get("digest", "")
                if digest:
                    task = f"[Autocompacted prior standby context (~{old_tok // 1000}K tok): {digest}]\n\n{task}"
                sb_entry = {**sb_entry, "turns": 1, "resumed": False, "compacted_from": old_tok}
            else:
                conv_id = sb_entry["conversation_id"]
                sb_entry = {**sb_entry, "turns": sb_entry.get("turns", 1) + 1, "resumed": True}

    # Inside a Claude Code Bash tool call (non-TTY with CLAUDE_CODE_SESSION_ID set), the
    # PreToolUse and PostToolUse hooks render the full flow graph via `systemMessage` (0 tokens!).
    # Only print the ASCII box to stdout when running interactively in a terminal or CR_FLOW_STDOUT=1.
    in_claude_tool = bool(os.environ.get("CLAUDE_CODE_SESSION_ID")) and not sys.stdout.isatty() \
        and os.environ.get("CR_FLOW_STDOUT") != "1"

    if not in_claude_tool:
        print(flow.dispatch_banner(
            model, "Antigravity", reason=reason, remapped_from=remapped_from, standby_info=sb_entry
        ), flush=True)

    preamble = ""
    comp_stats = None
    if conv_id:
        if not in_claude_tool:
            print(f"  ├─ ♻ reusing warm standby conversation {conv_id[:8]} (0 preamble tok; closes after {STANDBY_TURNS} idle turns)",
                  flush=True)
    elif not bare_requested(a.bare):
        if sb_entry and sb_entry.get("compacted_from") and not in_claude_tool:
            print(f"  ├─ ♻ autocompacted warm standby ({sb_entry['compacted_from'] // 1000}K tok → fresh session with digest)",
                  flush=True)
        preamble, comp_stats = build_context(task=task, use_compress=not a.full_context)
        if preamble and not in_claude_tool:
            print(f"  ├─ {context_line(comp_stats)}", flush=True)

    t0 = time.time()
    prog_fn = None if in_claude_tool else (lambda m: print(m, flush=True))
    if conv_id:
        result = run(task, model, a.effort, a.timeout, prog_fn, preamble,
                     reason=reason, remapped_from=remapped_from, conversation_id=conv_id,
                     comp_stats=comp_stats, standby_info=sb_entry)
    else:
        result = run(task, model, a.effort, a.timeout, prog_fn, preamble,
                     reason=reason, remapped_from=remapped_from, comp_stats=comp_stats,
                     standby_info=sb_entry)
    # If resuming a stale conversation failed, retry once fresh with preamble!
    if conv_id and result.get("status") != "SUCCESS" and not a.conversation:
        if not in_claude_tool:
            print("  ├─ ↻ warm standby expired upstream; starting fresh agy session", flush=True)
        conv_id, sb_entry = None, None
        if not bare_requested(a.bare):
            preamble, comp_stats = build_context(task=task, use_compress=not a.full_context)
        result = run(task, model, a.effort, a.timeout, prog_fn, preamble,
                     reason=reason, remapped_from=remapped_from, comp_stats=comp_stats)

    if standby_enabled and result.get("status") == "SUCCESS" and result.get("conversation_id"):
        u = result.get("usage") or {}
        ctx_tok = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_tokens", 0) or 0)
        saved_sb = standby_record(
            model, result["conversation_id"],
            context_tokens=ctx_tok, response_text=result.get("response") or "",
            provider="Antigravity",
        )
        if sb_entry and sb_entry.get("resumed") and saved_sb:
            sb_entry = {**saved_sb, "resumed": True}
        elif saved_sb:
            sb_entry = {
                **saved_sb,
                "resumed": False,
                "compacted_from": (sb_entry or {}).get("compacted_from"),
            }

    elapsed = result.get("duration_seconds") or (time.time() - t0)
    if not in_claude_tool:
        print(flow.completion_box(
            model=model,
            provider="Antigravity",
            status=result.get("status", "?"),
            seconds=float(elapsed),
            usage=result.get("usage") or {},
            reason=reason,
            remapped_from=remapped_from,
            comp_stats=comp_stats,
            standby_info=sb_entry,
            tier=eff_tier,
            balance=bal,
        ), flush=True)
    print((result.get("response") or "").strip())
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
