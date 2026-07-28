import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nltbuild.core.base import BaseBuild


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class PushTest(unittest.TestCase):
    def test_push_commits_oldest_files_in_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            remote = Path(temp) / "remote.git"
            repo.mkdir()
            git(temp, "init", "--bare", str(remote))
            git(repo, "init")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "remote", "add", "origin", str(remote))
            (repo / "README").write_text("initial\n")
            git(repo, "add", "README")
            git(repo, "commit", "-m", "initial")
            git(repo, "push", "-u", "origin", "HEAD")
            branch = git(repo, "branch", "--show-current")

            for index in range(21):
                path = repo / f"file-{index:02}.txt"
                path.write_text(f"{index}\n")
                os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
            git(repo, "add", "file-20.txt")

            builder = BaseBuild.__new__(BaseBuild)
            builder.repo_path = str(repo)
            builder.name = "repo"
            with patch("nltbuild.core.base.opencommit_commit", return_value=False):
                builder.push(message="batch", batch_size=20)

            commits = git(repo, "rev-list", "--reverse", "HEAD").splitlines()
            first_batch = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commits[1]).splitlines()
            self.assertEqual(first_batch, [f"file-{index:02}.txt" for index in range(20)])
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD"), "3")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", f"origin/{branch}"))


if __name__ == "__main__":
    unittest.main()
