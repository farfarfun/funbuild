import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core import util
from funbuild.core.base import BaseBuild

AICOMMITS_MESSAGE = "chore: ai generated message"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@contextlib.contextmanager
def stub_aicommits(bin_dir):
    """在 PATH 前置一个仿真 aicommits: 和真品一样无视外部信息, 用自己生成的信息提交。"""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "aicommits"
    script.write_text(f'#!/bin/sh\ngit commit -q -m "{AICOMMITS_MESSAGE}"\n', encoding="utf-8")
    script.chmod(0o755)
    util._aicommits_available.cache_clear()
    original = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original}"
    try:
        yield
    finally:
        os.environ["PATH"] = original
        util._aicommits_available.cache_clear()


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
            (repo / ".gitignore").write_text("file-20.txt\n")
            git(repo, "add", "README", ".gitignore")
            git(repo, "commit", "-m", "initial")
            git(repo, "push", "-u", "origin", "HEAD")
            branch = git(repo, "branch", "--show-current")

            for index in range(21):
                path = repo / f"file-{index:02}.txt"
                path.write_text(f"{index}\n")
                os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
            git(repo, "add", "-f", "file-20.txt")

            builder = BaseBuild.__new__(BaseBuild)
            builder.repo_path = str(repo)
            builder.name = "repo"
            with patch("funbuild.core.base.aicommits_commit", return_value=False):
                builder.push(message="batch", batch_size=20)

            commits = git(repo, "rev-list", "--reverse", "HEAD").splitlines()
            first_batch = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commits[1]).splitlines()
            self.assertEqual(first_batch, [f"file-{index:02}.txt" for index in range(20)])
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD"), "3")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", f"origin/{branch}"))


class CommitMessageTest(unittest.TestCase):
    """显式传入的 commit 信息必须被使用。

    回归: push 无条件调用 aicommits, 而 aicommits 完全无视外部传入的信息、
    自己生成一条, 于是 `funbuild build "我的信息"` 提交出来的是别的内容。
    """

    @contextlib.contextmanager
    def repo(self):
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
            (repo / "change.txt").write_text("changed\n")

            builder = BaseBuild.__new__(BaseBuild)
            builder.repo_path = str(repo)
            builder.name = "repo"
            with stub_aicommits(Path(temp) / "bin"):
                yield builder, repo

    def subjects(self, repo):
        return git(repo, "log", "--format=%s").splitlines()

    def test_explicit_message_is_used_verbatim(self):
        with self.repo() as (builder, repo):
            builder.push(message="修复了版本解析的边界问题")
            subjects = self.subjects(repo)
        self.assertEqual(subjects[0], "修复了版本解析的边界问题")
        self.assertNotIn(AICOMMITS_MESSAGE, subjects)

    def test_omitted_message_lets_aicommits_generate(self):
        with self.repo() as (builder, repo):
            builder.push()
            subjects = self.subjects(repo)
        self.assertEqual(subjects[0], AICOMMITS_MESSAGE)

    def test_build_forwards_explicit_message(self):
        """build 是 push 的调用方, 信息必须一路透传下去。"""
        with self.repo() as (builder, repo):
            with (
                patch.object(BaseBuild, "pull"),
                patch.object(BaseBuild, "upgrade"),
                patch.object(BaseBuild, "tags"),
                patch("funbuild.core.base.run_checked"),
            ):
                builder.build(message="发布 1.2.3")
            subjects = self.subjects(repo)
        self.assertEqual(subjects[0], "发布 1.2.3")

    def test_empty_batch_does_not_abort_push(self):
        """aicommits 会提交全部暂存内容, 后续批次可能无内容可提交, 不该让 push 失败。"""
        with self.repo() as (builder, repo):
            for index in range(3):
                (repo / f"extra-{index}.txt").write_text(f"{index}\n")
            branch = git(repo, "branch", "--show-current")
            builder.push(batch_size=1)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", f"origin/{branch}"))


class PushAllTest(unittest.TestCase):
    """funbuild push all: 先在每个 submodule 里执行 push, 再 push 当前仓库。"""

    def test_push_all_pushes_submodule_then_main_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            sub_remote = temp_path / "sub_remote.git"
            main_remote = temp_path / "main_remote.git"
            git(temp, "init", "--bare", str(sub_remote))
            git(temp, "init", "--bare", str(main_remote))

            sub_src = temp_path / "sub_src"
            sub_src.mkdir()
            git(sub_src, "init")
            git(sub_src, "config", "user.name", "Test")
            git(sub_src, "config", "user.email", "test@example.com")
            (sub_src / "s.txt").write_text("initial\n")
            git(sub_src, "add", "s.txt")
            git(sub_src, "commit", "-m", "sub initial")
            git(sub_src, "remote", "add", "origin", str(sub_remote))
            git(sub_src, "push", "-u", "origin", "HEAD")

            main_src = temp_path / "main_src"
            main_src.mkdir()
            git(main_src, "init")
            git(main_src, "config", "user.name", "Test")
            git(main_src, "config", "user.email", "test@example.com")
            git(main_src, "remote", "add", "origin", str(main_remote))
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub_remote), "sub"],
                cwd=main_src,
                check=True,
            )
            git(main_src, "commit", "-m", "add submodule")
            git(main_src, "push", "-u", "origin", "HEAD")

            (main_src / "sub" / "s.txt").write_text("initial\nchanged\n")
            (main_src / "top.txt").write_text("top\n")

            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            (fake_bin / "funbuild").write_text(
                "#!/bin/sh\n"
                f'exec env PYTHONPATH="{Path(__file__).resolve().parents[1] / "src"}" '
                "python3 -c \"import sys; sys.argv=['funbuild']+sys.argv[1:]; "
                'from funbuild.core.cli import funbuild; funbuild()" "$@"\n',
                encoding="utf-8",
            )
            (fake_bin / "funbuild").chmod(0o755)

            builder = BaseBuild.__new__(BaseBuild)
            builder.repo_path = str(main_src)
            builder.name = "main_src"

            original_path = os.environ["PATH"]
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path}"
            try:
                builder.push_all(message="push all commit", batch_size=20)
            finally:
                os.environ["PATH"] = original_path

            sub_branch = git(sub_src, "branch", "--show-current")
            self.assertEqual(
                git(main_src / "sub", "rev-parse", "HEAD"),
                git(temp_path / "sub_remote.git", "rev-parse", sub_branch),
            )
            main_branch = git(main_src, "branch", "--show-current")
            self.assertEqual(
                git(main_src, "rev-parse", "HEAD"),
                git(temp_path / "main_remote.git", "rev-parse", main_branch),
            )
            self.assertIn("top.txt", git(main_src, "show", "--stat", "HEAD"))


class TagsTest(unittest.TestCase):
    """回归: `git tag --force` 移动了本地 tag, 但 `git push --tags` 拒绝更新远端
    已存在的 tag, 于是重发同一版本会在发布成功之后卡在打 tag 这步失败。"""

    @contextlib.contextmanager
    def repo(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            remote = Path(temp) / "remote.git"
            repo.mkdir()
            git(temp, "init", "--bare", str(remote))
            git(repo, "init")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "remote", "add", "origin", str(remote))
            (repo / "f").write_text("1\n")
            git(repo, "add", "f")
            git(repo, "commit", "-m", "c1")
            git(repo, "push", "-u", "origin", "HEAD")

            builder = BaseBuild.__new__(BaseBuild)
            builder.repo_path = str(repo)
            builder.name = "repo"
            builder.version = "1.0.0"
            cwd = os.getcwd()
            os.chdir(repo)
            try:
                yield builder, repo, remote
            finally:
                os.chdir(cwd)

    def remote_tag(self, remote, tag):
        return subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", tag],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_tag_is_pushed(self):
        with self.repo() as (builder, repo, remote):
            builder.tags()
            self.assertEqual(self.remote_tag(remote, "v1.0.0"), git(repo, "rev-parse", "HEAD"))

    def test_retagging_same_version_moves_remote_tag(self):
        """重发同一版本时, 远端 tag 必须跟着移动而不是报错。"""
        with self.repo() as (builder, repo, remote):
            builder.tags()
            (repo / "f").write_text("2\n")
            git(repo, "commit", "-am", "c2")
            builder.tags()
            self.assertEqual(self.remote_tag(remote, "v1.0.0"), git(repo, "rev-parse", "HEAD"))

    def test_unrelated_local_tags_are_not_pushed(self):
        """`git push --tags` 会把本地所有 tag 都推上去, 只该推本次这一个。"""
        with self.repo() as (builder, repo, remote):
            git(repo, "tag", "scratch-local-only")
            builder.tags()
            remote_tags = subprocess.run(
                ["git", "--git-dir", str(remote), "tag", "-l"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            self.assertEqual(remote_tags, ["v1.0.0"])


if __name__ == "__main__":
    unittest.main()
