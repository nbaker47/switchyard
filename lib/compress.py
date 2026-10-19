"""Multi-layer token compression for switchyard (Caveman + Chain-of-Draft + TOON + RTK).

Layers:
1. Caveman / TOON Markdown Compressor (`compress_markdown` & `compact_claude_md`): shrinks
   CLAUDE.md and instruction preambles by joining wrapped lines, stripping conversational filler
   and stop words, using dense shorthand, and collapsing irrelevant sections into 1-line pointers
   while preserving every code block, path, command, URL, env var and rule verbatim.
2. Smart Skill Pruning (`filter_skills`): instead of listing all 40+ skills on every `cr agy`
   call, matches skills relevant to the task and condenses the rest to a 1-line index.
3. Chain-of-Draft + Telegraphic Wire Protocol (`COD_WIRE_RULES`): keeps subagent and `cr agy`
   reasoning and return payloads compact (<=5 words per reasoning step; structured return).
4. RTK CLI Output Compression (`rtk_wrap`): wraps noisy shell commands (git, pytest, cargo,
   go test, npm/pnpm/bun test, ruff, tsc, ls, grep) with `rtk` when installed.
"""
import os
import re
import shutil

# Conversational filler & verbose phrases replaced with Caveman/TOON shorthand outside code spans.
FILLER_PATTERNS = [
    (r"\bplease make sure (that )?(you )?", ""),
    (r"\bmake sure (that )?(you )?", ""),
    (r"\bit is important to (note|remember) that\b", ""),
    (r"\bin order to\b", "to"),
    (r"\bfor the purpose of\b", "for"),
    (r"\bat the end of every response,?\s*", "end every reply: "),
    (r"\bat all times\b", "always"),
    (r"\bwhenever possible\b", ""),
    (r"\bas a general rule,?\s*", ""),
    (r"\bkeep in mind that\b", ""),
    (r"\byou should always\b", "always"),
    (r"\byou must always\b", "must"),
    (r"\byou are required to\b", "must"),
    (r"\bdo not hesitate to\b", ""),
    (r"\bthat is to say,?\s*", "i.e. "),
    (r"\bin other words,?\s*", "i.e. "),
    (r"\bwhether or not\b", "whether"),
    (r"\bdue to the fact that\b", "bc"),
    (r"\bbecause of the fact that\b", "bc"),
    (r"\bin the event that\b", "if"),
    (r"\bprior to\b", "before"),
    (r"\bsubsequent to\b", "after"),
    (r"\bwith the exception of\b", "except"),
    (r"\bin addition to\b", "+"),
    (r"\bas well as\b", "&"),
    (r"\band/or\b", "/"),
    (r"\bfor example\b", "e.g."),
    (r"\bsuch as\b", "e.g."),
    (r"\bwithout\b", "w/o"),
    (r"\bwith\b", "w/"),
    (r"\bbecause\b", "bc"),
    (r"\bconfiguration\b", "cfg"),
    (r"\bdirectory\b", "dir"),
    (r"\bdirectories\b", "dirs"),
    (r"\brepository\b", "repo"),
    (r"\brepositories\b", "repos"),
    (r"\benvironment\b", "env"),
]

# Stop words safe to drop in Caveman prose (outside backticks, quotes, paths and URLs).
STOP_WORDS = re.compile(
    r"(?<=\s)(?:the|a|an|that|which|is|are|was|were|be|been|being|very|really|just|actually|basically|simply)(?=\s+[a-z]{2,}\b)",
    re.IGNORECASE,
)
INLINE_SPAN = re.compile(r"(`[^`]+`|\"[^\"]+\"|'[^']+'|https?://\S+|[@~/][\w./\-]+)")

COD_WIRE_RULES = (
    "Token discipline (Chain-of-Draft + Caveman Wire Protocol): "
    "(1) Internal reasoning/drafting: <=5 words per step (`file.py:42 -> nil ref -> guard`). "
    "(2) Drop filler, pleasantries, hedging, and prompt restatements; NEVER add a `route:` footer at the end of your reply (the TUI banner shows the route). "
    "(3) Always pass `--why \"<task + why this model over alternative>\"` on `cr agy` calls. "
    "(4) Keep all code, paths, commands, URLs and requested drafts exact."
)

# Section titles in CLAUDE.md that only apply when the task matches their keywords.
CONDITIONAL_SECTIONS = [
    ("resume command", {"resume", "crash"}, None),  # never needed in headless `cr agy`
    ("agent teams", {"team", "teams", "sendmessage", "teammate"}, None),
    ("writing emails", {"email", "gmail", "draft", "reply", "inbox", "mail", "outreach"},
     "Follow `~/Code/claude-harness/docs/email-scripture.md` (draft in chat first, never send)."),
    ("claude-mem", {"claude-mem", "chroma", "disk", "observer", "sqlite"},
     "If disk low or `~/.claude-mem` >5GB, see `~/Code/docs/claude-mem-observer-loop.md`."),
    ("cloud accounts", {"cloud", "aws", "gcp", "gcloud", "expo", "billing", "cost", "hosted", "account", "sheet"},
     "Cloud inventory sheet: https://docs.google.com/spreadsheets/d/1BxiQsKKczgOitNkE8C6edDIYzA_jrXnY1Rsa1jPha9A/edit"),
    ("cmux layout", {"cmux", "layout", "workspace", "restore-layout", "dock", "pane", "tab"},
     "Never close/rename pinned cmux workspaces or kill by pattern (`pkill`/`killall`); restore `~/dotfiles/cmux/layout.json` only when asked."),
    ("which chrome is which", {"chrome", "browser", "nathan", "peng"}, None),
]

# Commands that RTK compresses well when invoked as a standalone command.
RTK_SAFE_PREFIXES = (
    "git status", "git diff", "git log", "git show", "git branch",
    "pytest", "cargo test", "cargo check", "cargo clippy",
    "go test", "npm test", "pnpm test", "bun test", "vitest", "jest",
    "ruff check", "tsc ", "eslint ", "docker ps", "docker logs",
)


def _compress_prose_segment(seg):
    """Compress a plain-text segment (not inside backticks, quotes, paths or URLs)."""
    s = " " + seg + " "
    for pat, repl in FILLER_PATTERNS:
        s = re.sub(pat, repl, s, flags=re.IGNORECASE)
    s = STOP_WORDS.sub("", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _compress_line(line):
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith(("#", "|", "@", "```", "~~~")):
        return stripped
    bullet = ""
    m = re.match(r"^(\s*[-*+]\s+|\s*\d+\.\s+)", line)
    if m:
        bullet = "- " if m.group(1).strip() in ("-", "*", "+") else m.group(1).lstrip()
        stripped = line[m.end():].strip()
    parts = INLINE_SPAN.split(stripped)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
        else:
            comp = _compress_prose_segment(part)
            if comp:
                out.append(comp)
    joined = " ".join(p for p in out if p)
    joined = re.sub(r"\s+([,.;:!?)\]])", r"\1", joined)
    joined = re.sub(r"([(\[])\s+", r"\1", joined)
    return (bullet + joined).strip()


def _unwrap_paragraphs(lines):
    """Join hard-wrapped prose/bullet continuation lines outside code fences."""
    out = []
    in_fence = False
    for line in lines:
        s = line.strip()
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line.rstrip())
            continue
        if in_fence:
            out.append(line.rstrip())
            continue
        if not s:
            out.append("")
            continue
        if s.startswith(("#", "|", "@", "-", "*", "+")) or re.match(r"^\d+\.\s", s):
            out.append(s)
        elif out and out[-1] and not out[-1].startswith(("#", "|", "@", "```", "~~~")):
            out[-1] = out[-1] + " " + s
        else:
            out.append(s)
    return out


def compress_markdown(text, task=None):
    """Caveman/TOON-style markdown compressor for CLAUDE.md and instruction files.
    Preserves fenced code blocks, paths, inline code and structure while cutting prose tokens.
    When `task` is provided, also condenses sections irrelevant to `task` into 1-line rules.
    """
    if not text:
        return ""
    if task is not None:
        text = _prune_sections(text, task)
    lines = _unwrap_paragraphs(text.splitlines())
    out = []
    in_fence = False
    prev_blank = False
    for line in lines:
        s = line.strip()
        if s.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line.rstrip())
            prev_blank = False
            continue
        if in_fence:
            out.append(line.rstrip())
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        comp = _compress_line(line)
        if not comp:
            if not prev_blank and out:
                out.append("")
                prev_blank = True
            continue
        prev_blank = False
        out.append(comp)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def _prune_sections(text, task):
    """Condense sections of CLAUDE.md that don't match keywords in `task`."""
    task_words = _tokens_set(task)
    sections = re.split(r"(?m)^(?=##\s+)", text)
    kept = []
    for sec in sections:
        if not sec.startswith("## "):
            kept.append(sec)
            continue
        first_line, _, _ = sec.partition("\n")
        head_low = first_line.lower()
        matched_rule = None
        for marker, keywords, stub in CONDITIONAL_SECTIONS:
            if marker in head_low:
                if task_words & keywords:
                    matched_rule = "keep"
                else:
                    matched_rule = ("stub", first_line, stub)
                break
        if matched_rule is None or matched_rule == "keep":
            kept.append(sec)
        else:
            _, heading, stub = matched_rule
            if stub:
                kept.append(f"{heading}\n- {stub}\n")
    return "".join(kept)


def _tokens_set(text):
    return {w for w in re.findall(r"[a-z0-9_-]{3,}", (text or "").lower())
            if w not in {"the", "and", "for", "with", "this", "that", "from", "into", "your",
                         "use", "when", "can", "all", "are", "not", "you", "any", "how"}}


DOMAIN_HINTS = {
    "ui": {"frontend", "design", "taste", "minimalist", "brutalist", "stitch", "redesign", "css", "web", "mobile", "react", "component", "layout", "page"},
    "test": {"qa", "gate", "verify", "pytest", "check"},
    "ship": {"pr", "commit", "push", "deploy", "release", "merge", "git"},
    "flight": {"flights", "travel", "airline", "airport"},
    "image": {"imagegen", "visual", "brandkit", "logo", "screenshot"},
}


def filter_skills(found, refs, task="", max_skills=6):
    """Filter `[(name, desc, path)]` to skills relevant to `task`.
    If `task` is empty, returns all skills unchanged (preserving `--show-context` behavior).
    """
    if not task or not found or len(found) <= max_skills:
        return found, refs, 0
    task_low = task.lower()
    task_words = _tokens_set(task)
    expanded = set(task_words)
    for key, syns in DOMAIN_HINTS.items():
        if key in task_words or (task_words & syns):
            expanded |= syns | {key}

    scored = []
    for item in found:
        name, desc, path = item
        if name.lower() in task_low:
            scored.append((100, item))
            continue
        skill_words = _tokens_set(f"{name} {desc}")
        overlap = len(expanded & skill_words)
        if overlap > 0:
            scored.append((overlap, item))

    scored.sort(key=lambda x: (-x[0], x[1][0]))
    kept = [item for _, item in scored[:max_skills]]
    if not kept:
        priority = {"brief", "qa-gate", "ship"}
        kept = [it for it in found if it[0] in priority][:3] or found[:2]

    kept_dirs = {os.path.dirname(it[2]) for it in kept}
    filtered_refs = {k: v for k, v in (refs or {}).items()
                     if any(k.startswith(d) or d.startswith(k) for d in kept_dirs)}
    omitted = max(0, len(found) - len(kept))
    return kept, filtered_refs, omitted


def rtk_available():
    return bool(shutil.which("rtk"))


def rtk_wrap(cmd):
    """If `rtk` is installed and `cmd` is a single safe dev command without pipes/redirects,
    prefix it with `rtk ` to compress stdout 60-90%.
    """
    if not cmd or os.environ.get("CR_NO_RTK") == "1" or not rtk_available():
        return cmd
    stripped = cmd.strip()
    if stripped.startswith("rtk ") or any(op in stripped for op in ("|", ";", "&&", "||", ">", "<", "`", "$(")):
        return cmd
    low = stripped.lower()
    if any(low == p.strip() or low.startswith(p) for p in RTK_SAFE_PREFIXES):
        return f"rtk {stripped}"
    return cmd
