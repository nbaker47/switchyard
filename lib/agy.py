"""Hand a task to the Antigravity CLI (`agy`) and print only its answer.

It runs your own, already signed-in `agy` in print mode. Your Antigravity login is never
read or forwarded. Models are never hard-coded: a tier is matched against the list that
`agy models` returns for your account, so new models are picked up automatically.

    cr agy [--tier fast|cheap|strong|opus|sonnet|<model id>] [--effort LEVEL] [--bare] "task"
    cr agy --show-context
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


def _render(files, replaced, found, refs, memory, note):
    parts = [FRAME]
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
        if refs:
            lines.append("\nReference files (read when a skill points to them):")
            for folder in sorted(refs):
                lines.append(f"{folder}/")
                lines += [f"  {p}" for p in refs[folder]]
        parts.append("\n".join(lines))
    if memory:
        parts.append(f"## Memory index\n### {memory[0]}\n{memory[1].strip()}")
    return "\n\n".join(parts) + "\n"


def build_context(cwd=None, home=None, max_chars=None):
    """The preamble for agy, and its stats. Empty text when there is nothing to pass on."""
    cwd = cwd or os.getcwd()
    home = home or str(Path.home())
    if max_chars is None:
        max_chars = int(os.environ.get("CR_AGY_CONTEXT_MAX_CHARS") or CONTEXT_MAX_CHARS)
    files = claude_md_files(cwd, home)
    found, refs = skills(cwd, home)
    stats = {"claude_md": len(files), "skills": len(found), "chars": 0, "replaced": 0}
    if not files and not found:
        return "", stats
    memory = memory_index(cwd, home)
    replaced, note = set(), ""
    text = _render(files, replaced, found, refs, memory, note)
    # Over the cap: swap the largest CLAUDE.md files for a pointer, one at a time.
    order = sorted(range(len(files)), key=lambda i: -len(files[i][1]))
    while len(text) > max_chars and len(replaced) < len(order):
        replaced.add(order[len(replaced)])
        names = ", ".join(files[i][0] for i in sorted(replaced))
        note = (f"Note: context cap of {max_chars} chars exceeded; these files are not inlined, "
                f"read them yourself: {names}")
        text = _render(files, replaced, found, refs, memory, note)
    stats.update(chars=len(text), replaced=len(replaced))
    return text, stats


def context_line(stats):
    return (f"context: {stats['claude_md']} CLAUDE.md, {stats['skills']} skills, "
            f"~{stats['chars'] / 4000:.1f}k tokens (chars/4)"
            + (f", {stats['replaced']} CLAUDE.md pointed to, not inlined" if stats["replaced"] else ""))


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


def run(task, model, effort=None, timeout=0, progress=print, preamble=""):
    """Run agy, reporting each tool step as it happens, and return its final result."""
    cmd = ["agy", "--model", model, "--output-format", "stream-json"]
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
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                if shown < MAX_PROGRESS:
                    progress(f"  · {time.time() - started:4.0f}s  {describe(step)}")
                    shown += 1
                else:
                    hidden += 1
    if hidden:
        progress(f"  · … and {hidden} more steps")
    if not result:
        result = {"status": "ERROR", "response": "".join(other).strip() or "agy returned nothing"}
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
    p.add_argument("--effort")
    p.add_argument("--timeout", type=int, default=0, help="seconds; 0 waits for the turn")
    p.add_argument("--list", action="store_true", help="list models and how tiers resolve")
    p.add_argument("--bare", action="store_true",
                   help="no project context preamble (also CR_AGY_BARE=1)")
    p.add_argument("--show-context", action="store_true",
                   help="print the preamble that would be sent, and exit")
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
        text, stats = build_context()
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
                print(f"{t:>7} -> {pick(t, models)}")
            except SystemExit:
                print(f"{t:>7} -> (none)")
        return 0
    task = " ".join(a.task) or sys.stdin.read()
    if not task.strip():
        p.error("no task given")
    model = pick(a.tier, models)
    print(f"[agy {model}] started", flush=True)
    preamble = ""
    if not bare_requested(a.bare):
        preamble, stats = build_context()
        if preamble:
            print(context_line(stats), flush=True)
    result = run(task, model, a.effort, a.timeout, lambda m: print(m, flush=True), preamble)
    print(f"[agy {model} · {result.get('status', '?')} · {result.get('duration_seconds') or 0:.0f}s]")
    print((result.get("response") or "").strip())
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
