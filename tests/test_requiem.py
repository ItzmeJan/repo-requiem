"""Tests for Repo Requiem. Run with: python -m unittest discover -s tests"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requiem  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test Mortician")
        git(self.repo, "config", "user.email", "mortician@example.com")
        (self.repo / "README.md").write_text(
            "# Old Project\n\n![badge](https://example.com/badge.svg)\n\nA small tool.\n",
            encoding="utf-8",
        )
        (self.repo / "alive.py").write_text("print('hello')\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "feat: begin")

        (self.repo / "alive.py").unlink()
        (self.repo / "ghost.py").write_text("print('gone')\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "refactor: rename the soul")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collects_vital_signs(self) -> None:
        facts = requiem.collect_facts(self.repo)
        self.assertEqual(facts.name, self.repo.name)
        self.assertEqual(facts.commits, 2)
        self.assertEqual(facts.contributors, [("Test Mortician", 2)])
        self.assertIn("alive.py", facts.deleted_files)
        self.assertNotIn("ghost.py", facts.deleted_files)
        self.assertIn("alive.py", dict(facts.busiest_files))
        self.assertEqual(facts.branches, 1)
        self.assertIn("A small tool.", facts.readme)
        self.assertNotIn("badge.svg", facts.readme)
        self.assertEqual(facts.lifespan_days, 0)

    def test_rejects_non_repository(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(requiem.RequiemError):
                requiem.collect_facts(Path(empty))

    def test_cause_of_death_prefers_maintainer_exhaustion(self) -> None:
        facts = requiem.Facts(
            name="x",
            path=".",
            commits=5,
            contributors=[("solo", 5)],
            dormant_days=400,
        )
        self.assertEqual(requiem._cause_of_death(facts), "maintainer exhaustion")

    def test_cause_of_death_sees_the_long_silence(self) -> None:
        facts = requiem.Facts(
            name="x",
            path=".",
            commits=500,
            contributors=[("a", 300), ("b", 200)],
            dormant_days=365 * 6,
        )
        self.assertEqual(requiem._cause_of_death(facts), "the long silence")

    def test_cause_of_death_remembers_a_weekend_project(self) -> None:
        facts = requiem.Facts(
            name="x",
            path=".",
            commits=8,
            contributors=[("a", 5), ("b", 3)],
            lifespan_days=3,
            dormant_days=362,
        )
        self.assertEqual(requiem._cause_of_death(facts), "a weekend that got away")


class OutputTests(unittest.TestCase):
    def test_parses_json_inside_a_code_fence(self) -> None:
        raw = '```json\n{"epitaph": "quiet", "obituary": "It slept."}\n```'
        self.assertEqual(requiem.parse_json_object(raw)["epitaph"], "quiet")

    def test_parse_rejects_non_json(self) -> None:
        with self.assertRaises(requiem.RequiemError):
            requiem.parse_json_object("no tombstone here")

    def test_render_contains_the_evidence(self) -> None:
        facts = requiem.Facts(
            name="old-tool",
            path=".",
            commits=42,
            contributors=[("Ada", 42)],
            first_commit="2019-01-01",
            last_commit="2021-06-01",
            lifespan_days=882,
            dormant_days=1200,
            branches=2,
            tags=1,
            deleted_files=["a.py", "b.py"],
            busiest_files=[("a.py", 9)],
            readme="",
            cause_of_death="maintainer exhaustion",
        )
        eulogy = {
            "epitaph": "It compiled until it could not.",
            "obituary": "It was small and it was enough.",
            "cause_of_death": "the last commit",
            "last_words": "chore: goodnight",
            "survived_by": "a fork and a memory",
        }
        document = requiem.render(facts, eulogy, "test-model")
        self.assertIn("In Memoriam: old-tool", document)
        self.assertIn("It compiled until it could not.", document)
        self.assertIn("Ada (42)", document)
        self.assertIn("a.py", document)
        self.assertIn("test-model", document)

    def test_tombstone_is_a_box(self) -> None:
        art = requiem.tombstone("tiny", "2020 - 2021", "Here lies a side project")
        lines = art.splitlines()
        self.assertTrue(lines[0].strip().startswith("/"))
        self.assertTrue(lines[-1].strip().startswith("\\"))
        self.assertEqual(len({len(line) for line in lines}), 1)


if __name__ == "__main__":
    unittest.main()
