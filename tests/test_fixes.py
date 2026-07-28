"""P0 修复的回归测试。

每个用例对应一个曾经可复现的失败, 用于防止回归。
"""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nltbuild.core import util
from nltbuild.core.base import BaseBuild
from nltbuild.core.cli import nltbuild as cli_entry
from nltbuild.core.poetry_build import PoetryBuild
from nltbuild.core.registry import get_build
from nltbuild.core.util import ShellCommandError, parse_version, run_checked
from nltbuild.core.uv_build import UVBuild


def make_builder(cls=BaseBuild, version=None, repo_path="/tmp"):
    """绕过 __init__ 里的 git 调用构造 builder。"""
    builder = cls.__new__(cls)
    builder.repo_path = repo_path
    builder.name = "repo"
    builder.version = version
    return builder


class RunCheckedTest(unittest.TestCase):
    """构建/发布失败必须抛出, 不能被静默吞掉。"""

    def test_failing_command_raises(self):
        with self.assertRaises(ShellCommandError):
            run_checked(["exit 3"])

    def test_failure_stops_chain_and_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "after.txt"
            with self.assertRaises(ShellCommandError):
                run_checked(["false", f"touch {marker}"])
            self.assertFalse(marker.exists(), "失败后不应继续执行后续命令")

    def test_successful_chain_returns(self):
        run_checked(["true", "true"])

    def test_empty_command_list_is_noop(self):
        run_checked([])

    def test_build_does_not_push_when_build_fails(self):
        """核心回归: 构建失败时不得继续 push 和打 tag。"""
        builder = make_builder(version="1.0.0")
        with (
            patch.object(BaseBuild, "pull"),
            patch.object(BaseBuild, "_cmd_build", return_value=["exit 1"]),
            patch.object(BaseBuild, "_cmd_delete", return_value=[]),
            patch.object(BaseBuild, "_cmd_install", return_value=[]),
            patch.object(BaseBuild, "_cmd_publish", return_value=[]),
            patch.object(BaseBuild, "_write_version"),
            patch.object(BaseBuild, "push") as push,
            patch.object(BaseBuild, "tags") as tags,
        ):
            with self.assertRaises(ShellCommandError):
                builder.build()
            push.assert_not_called()
            tags.assert_not_called()


class ParseVersionTest(unittest.TestCase):
    """版本解析不得对非三段版本崩溃。"""

    def test_three_part_version(self):
        self.assertEqual(parse_version("1.6.54"), ([1, 6, 54], ""))

    def test_two_part_version_pads(self):
        self.assertEqual(parse_version("1.0"), ([1, 0, 0], ""))

    def test_single_part_version_pads(self):
        self.assertEqual(parse_version("2"), ([2, 0, 0], ""))

    def test_prerelease_suffix_is_split_off(self):
        self.assertEqual(parse_version("1.0.0rc1"), ([1, 0, 0], "rc1"))

    def test_v_prefix_accepted(self):
        self.assertEqual(parse_version("v1.2.3"), ([1, 2, 3], ""))

    def test_garbage_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_version("not-a-version")


class VersionUpgradeTest(unittest.TestCase):
    """__version_upgrade 曾对 1.0 抛 IndexError、对 1.0.0rc1 抛 ValueError。"""

    def upgrade(self, version):
        return make_builder(version=version)._BaseBuild__version_upgrade()

    def test_normal_increment(self):
        self.assertEqual(self.upgrade("1.6.54"), "1.6.55")

    def test_carry_at_step_boundary(self):
        self.assertEqual(self.upgrade("1.6.127"), "1.7.0")

    def test_two_part_version_does_not_crash(self):
        self.assertEqual(self.upgrade("1.0"), "1.0.1")

    def test_prerelease_does_not_crash(self):
        self.assertEqual(self.upgrade("1.0.0rc1"), "1.0.1")

    def test_none_version_defaults(self):
        self.assertEqual(self.upgrade(None), "0.0.2")


class PoetryCheckTypeTest(unittest.TestCase):
    """check_type 必须返回 bool, 不能抛 KeyError。"""

    def check_with_toml(self, content):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pyproject.toml"
            path.write_text(content, encoding="utf-8")
            builder = make_builder(PoetryBuild)
            builder.toml_path = str(path)
            return builder.check_type()

    def test_tool_section_without_poetry_returns_false(self):
        self.assertFalse(self.check_with_toml("[tool.ruff]\nline-length = 120\n"))

    def test_poetry_section_returns_true(self):
        self.assertTrue(self.check_with_toml('[tool.poetry]\nversion = "1.2.3"\n'))

    def test_poetry_version_is_read(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pyproject.toml"
            path.write_text('[tool.poetry]\nversion = "1.2.3"\n', encoding="utf-8")
            builder = make_builder(PoetryBuild)
            builder.toml_path = str(path)
            builder.check_type()
            self.assertEqual(builder.version, "1.2.3")

    def test_malformed_toml_returns_false(self):
        self.assertFalse(self.check_with_toml("this is not [valid toml"))

    def test_missing_file_returns_false(self):
        builder = make_builder(PoetryBuild)
        builder.toml_path = "/nonexistent/pyproject.toml"
        self.assertFalse(builder.check_type())


class RegistryTest(unittest.TestCase):
    def test_ruff_only_pyproject_resolves_without_crash(self):
        """曾因 PoetryBuild 抛 KeyError 而整条探测链中断。"""
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(temp)
            try:
                with patch("nltbuild.core.base.run_shell", return_value=temp):
                    builder = get_build()
            finally:
                os.chdir(cwd)
            self.assertIsNotNone(builder)

    def test_broken_builder_is_skipped_not_fatal(self):
        with patch.object(PoetryBuild, "check_type", side_effect=RuntimeError("boom")):
            with patch("nltbuild.core.base.run_shell", return_value="/tmp"):
                self.assertIsNotNone(get_build())


class PublishCredentialsTest(unittest.TestCase):
    """凭据必须走环境变量, 不能出现在命令行 (ps aux 可见)。"""

    def setUp(self):
        for key in ("UV_PUBLISH_TOKEN", "UV_PUBLISH_USERNAME", "UV_PUBLISH_PASSWORD", "UV_PUBLISH_URL"):
            os.environ.pop(key, None)
            self.addCleanup(os.environ.pop, key, None)

    def export(self, settings):
        make_builder(UVBuild)._export_publish_credentials(settings)

    def test_token_goes_to_env_not_argv(self):
        self.export({"username": "__token__", "password": "pypi-secret"})
        self.assertEqual(os.environ["UV_PUBLISH_TOKEN"], "pypi-secret")

    def test_username_password_go_to_env(self):
        self.export({"username": "alice", "password": "p@ss word'quote"})
        self.assertEqual(os.environ["UV_PUBLISH_USERNAME"], "alice")
        self.assertEqual(os.environ["UV_PUBLISH_PASSWORD"], "p@ss word'quote")

    def test_repository_url_goes_to_env(self):
        self.export({"username": "alice", "password": "x", "repository": "https://example.com/simple"})
        self.assertEqual(os.environ["UV_PUBLISH_URL"], "https://example.com/simple")

    def test_no_username_exports_nothing(self):
        self.export({})
        self.assertNotIn("UV_PUBLISH_TOKEN", os.environ)
        self.assertNotIn("UV_PUBLISH_USERNAME", os.environ)

    def test_credentials_absent_from_publish_commands(self):
        builder = make_builder(UVBuild, repo_path="/repo")
        builder.toml_paths = ["./pyproject.toml"]
        secret = "super-secret-token"
        with patch("os.path.exists", return_value=False):
            with patch.object(UVBuild, "_export_publish_credentials"):
                cmds = builder._cmd_publish()
        self.assertTrue(cmds)
        for cmd in cmds:
            self.assertNotIn(secret, cmd)
            self.assertNotIn("--token", cmd)
            self.assertNotIn("--password", cmd)
            self.assertNotIn("--username", cmd)


class AicommitsProbeTest(unittest.TestCase):
    """aicommits 不存在时只探测一次, 不应每批重试。"""

    def setUp(self):
        util._aicommits_available.cache_clear()
        self.addCleanup(util._aicommits_available.cache_clear)

    def test_missing_cli_probed_once(self):
        staged = type("R", (), {"returncode": 1})()
        with patch("nltbuild.core.util.shutil.which", return_value=None) as which:
            with patch("nltbuild.core.util.subprocess.run", return_value=staged) as run:
                for _ in range(5):
                    self.assertFalse(util.opencommit_commit("add"))

        self.assertEqual(which.call_count, 1, "aicommits 可用性只应探测一次")
        aicommits_calls = [c for c in run.call_args_list if c.args[0][0] == "aicommits"]
        self.assertEqual(aicommits_calls, [], "CLI 缺失时不应尝试调用")

    def test_available_cli_is_invoked(self):
        staged = type("R", (), {"returncode": 1})()
        with patch("nltbuild.core.util.shutil.which", return_value="/usr/bin/aicommits"):
            with patch("nltbuild.core.util.subprocess.run", return_value=staged) as run:
                util.opencommit_commit("add")
        self.assertTrue([c for c in run.call_args_list if c.args[0][0] == "aicommits"])


class ReleaseAliasTest(unittest.TestCase):
    """release 必须与 build 走同一条流水线。"""

    def invoke(self, argv):
        builder = MagicMock()
        with patch("nltbuild.core.cli.get_build", return_value=builder):
            with patch.object(sys, "argv", ["nltbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return builder

    def test_release_dispatches_to_build(self):
        builder = self.invoke(["release"])
        builder.build.assert_called_once_with(message="add")

    def test_release_accepts_positional_message(self):
        builder = self.invoke(["release", "ship it"])
        builder.build.assert_called_once_with(message="ship it")

    def test_release_matches_build(self):
        self.assertEqual(
            self.invoke(["release", "same"]).build.call_args,
            self.invoke(["build", "same"]).build.call_args,
        )


class TagCommandTest(unittest.TestCase):
    """标签命令已由 tags 重命名为 tag。"""

    def invoke(self, argv):
        builder = MagicMock()
        with patch("nltbuild.core.cli.get_build", return_value=builder):
            with patch.object(sys, "argv", ["nltbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return builder

    def test_tag_dispatches_to_builder(self):
        self.invoke(["tag"]).tags.assert_called_once_with()

    def test_old_tags_name_is_gone(self):
        self.invoke(["tags"]).tags.assert_not_called()


if __name__ == "__main__":
    unittest.main()
