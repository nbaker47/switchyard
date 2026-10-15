"""Give agy the same skills Claude Code has, by linking them into agy's skills folder.

    cr skills [--dry-run]

Claude Code reads skills from ~/.claude/skills (and ~/.agents/skills holds the shared copies);
the Antigravity CLI reads user skills from ~/.gemini/config/skills. Both use the same SKILL.md
format, so a symlink per skill is enough. Links only: nothing is copied, and a skill agy
already has under the same name is left alone. Re-run after adding skills.
"""
import argparse
import os
import sys
from pathlib import Path

SOURCES = [Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills"]
TARGET = Path(os.environ.get("CR_AGY_SKILLS", Path.home() / ".gemini" / "config" / "skills"))


def plan(sources=SOURCES, target=TARGET):
    """(name, source dir) for every skill agy doesn't have yet, first source winning."""
    have = {p.name for p in target.iterdir()} if target.exists() else set()
    todo = {}
    for src in sources:
        if not src.is_dir():
            continue
        for skill in sorted(src.iterdir()):
            real = skill.resolve()
            if (real / "SKILL.md").is_file() and skill.name not in have and skill.name not in todo:
                todo[skill.name] = real
    return todo


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr skills", description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    todo = plan()
    if not todo:
        print(f"agy already has every skill ({TARGET}).")
        return 0
    if not a.dry_run:
        TARGET.mkdir(parents=True, exist_ok=True)
    for name, src in todo.items():
        if not a.dry_run:
            (TARGET / name).symlink_to(src, target_is_directory=True)
        print(f"{'would link' if a.dry_run else 'linked'} {name} -> {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
