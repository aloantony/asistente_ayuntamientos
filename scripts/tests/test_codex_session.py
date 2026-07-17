from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "codex-session"


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


class CodexSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "project"

        git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        git(self.root, "init", "--initial-branch=main", str(self.seed))
        git(self.seed, "config", "user.name", "Codex Session Tests")
        git(self.seed, "config", "user.email", "codex-session@example.invalid")
        (self.seed / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.seed / ".gitignore").write_text(".codex-sessions/\n", encoding="utf-8")
        git(self.seed, "add", "README.md", ".gitignore")
        git(self.seed, "commit", "-m", "test: seed repository")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-u", "origin", "main")
        git(self.root, "clone", str(self.origin), str(self.repo))
        git(self.repo, "config", "user.name", "Codex Session Tests")
        git(self.repo, "config", "user.email", "codex-session@example.invalid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def helper(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return run(
            ["python3", str(SCRIPT), *args],
            cwd=cwd or self.repo,
            check=check,
        )

    def create_session(self, slug: str = "task-one") -> tuple[dict[str, object], Path]:
        self.helper("create", slug, "--owner", "worker-a")
        record_path = self.repo / ".codex-sessions" / slug / "session.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        return record, Path(str(record["path"]))

    def test_create_reserves_unique_branch_and_lists_session(self) -> None:
        record, worktree = self.create_session()

        self.assertTrue(worktree.is_dir())
        self.assertEqual(record["owner"], "worker-a")
        self.assertEqual(record["status"], "active")
        self.assertTrue(str(record["branch"]).startswith("codex/task-one-"))
        self.assertEqual(git(worktree, "branch", "--show-current").stdout.strip(), record["branch"])

        duplicate = self.helper(
            "create", "task-one", "--owner", "worker-b", check=False
        )
        self.assertEqual(duplicate.returncode, 2)

        listed = json.loads(self.helper("list", "--json").stdout)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["task"], "task-one")
        self.assertEqual(listed[0]["session_status"], "active")

    def test_handoff_ready_requires_clean_published_branch_and_tests(self) -> None:
        record, worktree = self.create_session("handoff")
        readme = worktree / "README.md"
        readme.write_text("fixture\nchanged\n", encoding="utf-8")

        dirty = self.helper(
            "handoff",
            "--status",
            "ready",
            "--test",
            "python3 -m unittest",
            cwd=worktree,
            check=False,
        )
        self.assertEqual(dirty.returncode, 2)
        self.assertIn("clean worktree", dirty.stderr)

        git(worktree, "add", "README.md")
        git(worktree, "commit", "-m", "test: change fixture")
        git(worktree, "push", "-u", "origin", "HEAD")

        missing_tests = self.helper(
            "handoff", "--status", "ready", cwd=worktree, check=False
        )
        self.assertEqual(missing_tests.returncode, 2)
        self.assertIn("at least one --test", missing_tests.stderr)

        self.helper(
            "handoff",
            "--status",
            "ready",
            "--test",
            "python3 -m unittest",
            cwd=worktree,
        )
        updated = json.loads(
            (
                self.repo / ".codex-sessions" / "handoff" / "session.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["tests"], ["python3 -m unittest"])
        self.assertEqual(updated["handoff_commit"], git(worktree, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(updated["branch"], record["branch"])

    def test_paused_handoff_keeps_dirty_worktree_reserved(self) -> None:
        _, worktree = self.create_session("paused-task")
        (worktree / "README.md").write_text("unfinished\n", encoding="utf-8")

        self.helper("handoff", "--status", "paused", cwd=worktree)

        listed = json.loads(self.helper("list", "--json").stdout)
        self.assertEqual(listed[0]["session_status"], "paused")
        self.assertEqual(listed[0]["dirty"], 1)

    def test_audit_marks_mass_deletions_as_quarantine_candidate(self) -> None:
        _, worktree = self.create_session("legacy-task")
        for index in range(12):
            (worktree / f"legacy-{index}.txt").write_text("legacy\n", encoding="utf-8")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "test: add legacy files")
        for index in range(12):
            (worktree / f"legacy-{index}.txt").unlink()

        rows = json.loads(self.helper("audit", "--json").stdout)
        legacy = next(row for row in rows if row["branch"] == self._branch(worktree))
        self.assertEqual(legacy["dirty"], 12)
        self.assertEqual(legacy["classification"], "quarantine-candidate")

    def test_runtime_lease_is_exclusive_and_owner_controlled(self) -> None:
        self.helper(
            "runtime",
            "acquire",
            "--owner",
            "coordinator",
            "--task",
            "coordination",
        )
        duplicate = self.helper(
            "runtime",
            "acquire",
            "--owner",
            "worker-b",
            "--task",
            "other-task",
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("already leased", duplicate.stderr)

        status = json.loads(self.helper("runtime", "status", "--json").stdout)
        self.assertEqual(status["owner"], "coordinator")
        wrong_owner = self.helper(
            "runtime", "release", "--owner", "worker-b", check=False
        )
        self.assertEqual(wrong_owner.returncode, 2)
        self.helper("runtime", "release", "--owner", "coordinator")
        self.assertEqual(
            self.helper("runtime", "status", "--json").stdout.strip(), "null"
        )

    @staticmethod
    def _branch(worktree: Path) -> str:
        return git(worktree, "branch", "--show-current").stdout.strip()


if __name__ == "__main__":
    unittest.main()
