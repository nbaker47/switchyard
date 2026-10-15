"""Run with: python3 -m unittest discover tests"""
import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("agy_ctx", ROOT / "lib/agy.py")
agy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agy)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.proj = self.root / "work" / "proj"
        self.sub = self.proj / "sub"
        self.sub.mkdir(parents=True)

    def tree(self):
        write(self.home / ".claude/CLAUDE.md", "GLOBAL")
        write(self.root / "work/CLAUDE.md", "OUTER")
        write(self.proj / "CLAUDE.md", "PROJECT @extra.md")
        write(self.proj / "CLAUDE.local.md", "LOCAL")
        write(self.sub / "CLAUDE.md", "INNER")
        write(self.home / ".claude/skills/g/SKILL.md",
              "---\nname: gskill\ndescription: Global one\n---\nBODY-G")
        write(self.proj / ".claude/skills/shared/flow.md", "flow ref")
        write(self.proj / ".claude/skills/deep/nested/SKILL.md",
              "---\nname: nested\ndescription: >-\n  folded\n  text\n---\nSECRET-BODY")

    def test_order_root_to_cwd(self):
        self.tree()
        text, stats = agy.build_context(self.sub, self.home, 10**6)
        pos = [text.index(m) for m in ("GLOBAL", "OUTER", "PROJECT", "LOCAL", "INNER")]
        self.assertEqual(pos, sorted(pos))
        self.assertIn(f"### {self.sub / 'CLAUDE.md'}", text)
        self.assertEqual(stats["claude_md"], 5)
        self.assertTrue(text.startswith("Project instructions loaded for you"))

    def test_import_inlined_once(self):
        write(self.proj / "CLAUDE.md", "A\n@extra.md\n@extra.md")
        write(self.proj / "extra.md", "EXTRA-TEXT")
        text, stats = agy.build_context(self.proj, self.home, 10**6)
        self.assertEqual(text.count("EXTRA-TEXT"), 1)

    def test_skills_listed_not_inlined(self):
        self.tree()
        text, stats = agy.build_context(self.sub, self.home, 10**6)
        self.assertEqual(stats["skills"], 2)
        self.assertIn("Skills available (read the file before using one)", text)
        self.assertIn("gskill: Global one", text)
        self.assertIn("nested: folded text", text)
        self.assertIn(str(self.proj / ".claude/skills/deep/nested/SKILL.md"), text)
        self.assertIn(str(self.proj / ".claude/skills/shared/flow.md"), text)
        self.assertNotIn("SECRET-BODY", text)
        self.assertNotIn("BODY-G", text)

    def test_memory_inlined(self):
        self.tree()
        slug = str(self.sub).replace("/", "-")
        write(self.home / ".claude/projects" / slug / "memory/MEMORY.md", "MEM-INDEX")
        text, _ = agy.build_context(self.sub, self.home, 10**6)
        self.assertIn("MEM-INDEX", text)

    def test_cap_replaces_largest(self):
        write(self.root / "work/CLAUDE.md", "small")
        write(self.proj / "CLAUDE.md", "B" * 5000)
        write(self.sub / "CLAUDE.md", "C" * 3000)
        text, stats = agy.build_context(self.sub, self.home, 4000)
        self.assertNotIn("B" * 100, text)
        self.assertIn("C" * 3000, text)
        self.assertIn("small", text)
        self.assertIn(f"### {self.proj / 'CLAUDE.md'}", text)  # still listed, as a pointer
        self.assertIn("context cap", text)
        self.assertEqual(stats["replaced"], 1)
        self.assertIn("1 CLAUDE.md pointed to", agy.context_line(stats))

    def test_noop_when_nothing(self):
        write(self.home / ".claude/projects/x/memory/MEMORY.md", "unused")
        text, stats = agy.build_context(self.sub, self.home, 10**6)
        self.assertEqual(text, "")
        self.assertEqual(stats["claude_md"], 0)

    def test_context_line(self):
        self.assertEqual(agy.context_line({"claude_md": 3, "skills": 41, "chars": 9200, "replaced": 0}),
                         "context: 3 CLAUDE.md, 41 skills, ~2.3k tokens (chars/4)")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        write(self.dir / "CLAUDE.md", "RULES-HERE")
        home = self.dir / "home"
        home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)

    def show(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(agy, "list_models", side_effect=AssertionError("agy called")), \
                mock.patch.object(agy, "run", side_effect=AssertionError("agy called")):
            self.assertEqual(agy.main(argv), 0)
        return out.getvalue(), err.getvalue()

    def test_show_context_exits_without_agy(self):
        out, err = self.show(["--show-context"])
        self.assertIn("RULES-HERE", out)
        self.assertIn("context: 1 CLAUDE.md", err)

    def test_bare_flag_skips(self):
        out, _ = self.show(["--show-context", "--bare"])
        self.assertNotIn("RULES-HERE", out)

    def test_bare_env_skips(self):
        out, _ = self.show(["--show-context"], {"CR_AGY_BARE": "1"})
        self.assertNotIn("RULES-HERE", out)

    def test_task_follows_preamble(self):
        captured = {}

        class Boom(Exception):
            pass

        def popen(cmd, **kw):
            captured["cmd"] = cmd
            raise Boom

        with mock.patch.object(agy.subprocess, "Popen", popen), self.assertRaises(Boom):
            agy.run("DO-IT", "m", preamble="PRE\n")
        prompt = captured["cmd"][-1]
        self.assertTrue(prompt.startswith("PRE"))
        self.assertTrue(prompt.endswith("## Task\nDO-IT"))
        with mock.patch.object(agy.subprocess, "Popen", popen), self.assertRaises(Boom):
            agy.run("DO-IT", "m")
        self.assertEqual(captured["cmd"][-1], "DO-IT")


if __name__ == "__main__":
    unittest.main()
