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

import tomlkit

from nltbuild.core import util
from nltbuild.core.base import BaseBuild, git_repo_root
from nltbuild.core.cli import nltbuild as cli_entry
from nltbuild.core.empty_build import EmptyBuild
from nltbuild.core.poetry_build import PoetryBuild
from nltbuild.core.registry import get_build
from nltbuild.core.util import NotAGitRepositoryError, ShellCommandError, parse_version, run_checked
from nltbuild.core.uv_build import UVBuild
from nltbuild.core.version_file_build import VersionFileBuild


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
            git_repo_root.cache_clear()
            try:
                with patch("nltbuild.core.base.run_shell", return_value=temp):
                    builder = get_build()
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()
            self.assertIsNotNone(builder)

    def test_broken_builder_is_skipped_not_fatal(self):
        git_repo_root.cache_clear()
        self.addCleanup(git_repo_root.cache_clear)
        # get_build 会 chdir 到仓库根, 这里没走 repo() 助手, 自己负责还原
        self.addCleanup(os.chdir, os.getcwd())
        with patch.object(PoetryBuild, "check_type", side_effect=RuntimeError("boom")):
            with patch("nltbuild.core.base.run_shell", return_value="/tmp"):
                self.assertIsNotNone(get_build())


class VersionFileBuildTest(unittest.TestCase):
    """根目录纯文本 VERSION 的仓库 (如 shell 项目) 应被识别, 而不是静默落到 EmptyBuild。"""

    @contextlib.contextmanager
    def repo(self, files):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in files.items():
                (Path(temp) / name).write_text(content, encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(temp)
            git_repo_root.cache_clear()
            try:
                with patch("nltbuild.core.base.run_shell", return_value=temp):
                    yield Path(temp)
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

    def test_version_file_repo_is_recognized(self):
        with self.repo({"VERSION": "0.1.7\n"}):
            builder = get_build()
        self.assertIsInstance(builder, VersionFileBuild)
        self.assertEqual(builder.version, "0.1.7")

    def test_v_prefix_is_stripped(self):
        """否则 tag 会拼成 vv0.1.7。"""
        with self.repo({"VERSION": "v0.1.7\n"}):
            self.assertEqual(get_build().version, "0.1.7")

    def test_upgrade_writes_back(self):
        with self.repo({"VERSION": "0.1.7\n"}) as root:
            get_build().upgrade()
            self.assertEqual((root / "VERSION").read_text().strip(), "0.1.8")

    def test_unparseable_version_file_falls_through(self):
        with self.repo({"VERSION": "not-a-version\n"}):
            self.assertNotIsInstance(get_build(), VersionFileBuild)

    def test_empty_version_file_falls_through(self):
        with self.repo({"VERSION": "\n"}):
            self.assertNotIsInstance(get_build(), VersionFileBuild)

    def test_pyproject_takes_precedence_over_version_file(self):
        manifest = '[project]\nname = "x"\nversion = "2.0.0"\n'
        with self.repo({"VERSION": "0.1.7\n", "pyproject.toml": manifest}):
            builder = get_build()
        self.assertNotIsInstance(builder, VersionFileBuild)
        self.assertEqual(builder.version, "2.0.0")

    def test_no_manifest_warns_before_falling_back(self):
        # nltlog 走 loguru, 不经 stdlib logging, assertLogs 抓不到它的输出,
        # 因此直接断言 logger 被调用。
        with self.repo({"README.md": "x\n"}):
            with patch("nltbuild.core.empty_build.logger") as log:
                builder = get_build()
        self.assertIsInstance(builder, EmptyBuild)
        self.assertTrue(any("未识别到版本清单" in str(call) for call in log.warning.call_args_list))


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
                    self.assertFalse(util.aicommits_commit())

        self.assertEqual(which.call_count, 1, "aicommits 可用性只应探测一次")
        aicommits_calls = [c for c in run.call_args_list if c.args[0][0] == "aicommits"]
        self.assertEqual(aicommits_calls, [], "CLI 缺失时不应尝试调用")

    def test_available_cli_is_invoked(self):
        staged = type("R", (), {"returncode": 1})()
        with patch("nltbuild.core.util.shutil.which", return_value="/usr/bin/aicommits"):
            with patch("nltbuild.core.util.subprocess.run", return_value=staged) as run:
                util.aicommits_commit()
        self.assertTrue([c for c in run.call_args_list if c.args[0][0] == "aicommits"])


class SanitizeCommitMessageTest(unittest.TestCase):
    """回归: aicommits 配了 deepseek-reasoner 等推理模型时, 生成的 commit 信息整条
    就是 `<think>`, 直接进了 git 历史。"""

    def clean(self, text):
        return util.sanitize_commit_message(text)

    def test_bare_think_tag_yields_empty(self):
        """实际发生过的情况: 正文只有 <think>, 无任何可用内容。"""
        self.assertEqual(self.clean("<think>\n"), "")

    def test_conclusion_after_closing_tag_is_kept(self):
        self.assertEqual(self.clean("<think>\n盘算一番\n</think>\nfix: 修正版本解析"), "fix: 修正版本解析")

    def test_unclosed_think_block_is_dropped(self):
        self.assertEqual(self.clean("<think>\n推理被截断了..."), "")

    def test_orphan_closing_tag_keeps_tail(self):
        self.assertEqual(self.clean("想了很久\n</think>\nfeat: 新增 X"), "feat: 新增 X")

    def test_markdown_fence_is_stripped(self):
        self.assertEqual(self.clean("```\nchore: 更新依赖\n```"), "chore: 更新依赖")

    def test_normal_message_is_untouched(self):
        self.assertEqual(self.clean("fix: 修了个 bug"), "fix: 修了个 bug")

    def test_multiline_body_is_preserved(self):
        self.assertEqual(self.clean("feat: 标题\n\n正文说明"), "feat: 标题\n\n正文说明")

    def test_empty_input_is_safe(self):
        self.assertEqual(self.clean(""), "")
        self.assertEqual(self.clean(None), "")


class RepairGeneratedMessageTest(unittest.TestCase):
    """信息被污染时必须就地 amend, 不能让它留在历史里。"""

    def repair(self, generated, fallback="add"):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["git", "log"]:
                return type("R", (), {"stdout": generated, "returncode": 0})()
            return type("R", (), {"stdout": "", "returncode": 0})()

        with patch("nltbuild.core.util.subprocess.run", side_effect=fake_run):
            util._repair_generated_message("/repo", fallback)
        return [c for c in calls if "--amend" in c]

    def test_think_only_message_is_amended_to_fallback(self):
        amends = self.repair("<think>\n", fallback="add")
        self.assertEqual(amends, [["git", "commit", "--amend", "-m", "add"]])

    def test_recoverable_message_is_amended_to_conclusion(self):
        amends = self.repair("<think>想了想</think>\nfix: 真正的信息")
        self.assertEqual(amends, [["git", "commit", "--amend", "-m", "fix: 真正的信息"]])

    def test_clean_message_is_left_alone(self):
        self.assertEqual(self.repair("fix: 一条正常的信息\n"), [])


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
        builder.build.assert_called_once_with(message=None)

    def test_release_accepts_positional_message(self):
        builder = self.invoke(["release", "ship it"])
        builder.build.assert_called_once_with(message="ship it")

    def test_release_matches_build(self):
        self.assertEqual(
            self.invoke(["release", "same"]).build.call_args,
            self.invoke(["build", "same"]).build.call_args,
        )


class RepoRootCacheTest(unittest.TestCase):
    """registry 会实例化 7 个 builder (hybrid 再建 2 个), 每个都跑一次
    `git rev-parse`, 单次 CLI 调用因此有 8 次 subprocess。现按 cwd 缓存为 1 次。"""

    @contextlib.contextmanager
    def repo(self, files):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in files.items():
                (Path(temp) / name).write_text(content, encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(temp)
            git_repo_root.cache_clear()
            try:
                yield Path(temp)
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

    def test_git_rev_parse_runs_once_per_detection(self):
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}) as root:
            with patch("nltbuild.core.base.run_shell", return_value=str(root)) as shell:
                get_build()
        self.assertEqual(shell.call_count, 1, f"git rev-parse 应只执行一次, 实际 {shell.call_count} 次")

    def test_repo_path_is_stripped(self):
        """未 strip 时 name 会带换行, 被拼进 project.urls 生成非法 URL。"""
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}) as root:
            with patch("nltbuild.core.base.run_shell", return_value=f"{root}\n"):
                builder = get_build()
        self.assertEqual(builder.repo_path, str(root))
        self.assertNotIn("\n", builder.name)


class RepoRootNormalizationTest(unittest.TestCase):
    """从子目录运行时, 清单探测和构建命令的相对路径全部落空, 会静默退化成
    EmptyBuild 且退出码为 0 —— 看起来发布成功了, 其实什么都没做。"""

    @contextlib.contextmanager
    def repo(self, files):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in files.items():
                path = Path(temp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            cwd = os.getcwd()
            git_repo_root.cache_clear()
            try:
                yield Path(temp)
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

    def test_detection_works_from_subdirectory(self):
        manifest = '[project]\nname = "x"\nversion = "2.0.0"\n'
        with self.repo({"pyproject.toml": manifest, "src/pkg/__init__.py": ""}) as root:
            os.chdir(root / "src" / "pkg")
            with patch("nltbuild.core.base.run_shell", return_value=str(root)):
                builder = get_build()
                self.assertEqual(os.getcwd(), str(root), "应已归一化到仓库根")
        self.assertNotIsInstance(builder, EmptyBuild)
        self.assertEqual(builder.version, "2.0.0")

    def test_outside_git_repo_raises_clear_error(self):
        """原先是 subprocess 抛 FileNotFoundError: '', 完全看不出真正原因。"""
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}) as root:
            os.chdir(root)
            with patch("nltbuild.core.base.run_shell", return_value=""):
                with self.assertRaises(NotAGitRepositoryError):
                    get_build()

    def test_git_root_pointing_nowhere_raises(self):
        with self.repo({}) as root:
            os.chdir(root)
            with patch("nltbuild.core.base.run_shell", return_value="/nonexistent/repo"):
                with self.assertRaises(NotAGitRepositoryError):
                    get_build()


class ConfigFormatTest(unittest.TestCase):
    """config_format 曾在 [project] 缺 description 时抛 KeyError, 使 upgrade 整个失败。"""

    def format_with(self, project_table):
        builder = make_builder(UVBuild)
        builder.name = "funx"
        config = {"project": project_table}
        builder.config_format(config)
        return config

    def test_missing_description_does_not_raise(self):
        config = self.format_with({"name": "funx", "version": "1.0.0"})
        self.assertEqual(config["project"]["description"], "funx")

    def test_placeholder_description_is_replaced(self):
        config = self.format_with({"description": "Add your description here"})
        self.assertEqual(config["project"]["description"], "funx")

    def test_real_description_is_preserved(self):
        config = self.format_with({"description": "a real description"})
        self.assertEqual(config["project"]["description"], "a real description")

    def test_non_fun_project_is_untouched(self):
        builder = make_builder(UVBuild)
        builder.name = "nltbuild"
        config = {"project": {"name": "nltbuild"}}
        builder.config_format(config)
        self.assertEqual(config, {"project": {"name": "nltbuild"}})


class LicenseMetadataTest(unittest.TestCase):
    """许可证声明改用 PEP 639: 旧的 [tool.setuptools] license-files = [] 既让 wheel
    不带许可证, 又会在 setuptools>=77 下与 [project].license-files 冲突报错。"""

    @contextlib.contextmanager
    def pkg(self, files):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in files.items():
                (Path(temp) / name).write_text(content, encoding="utf-8")
            yield temp

    def apply(self, config, pkg_dir):
        builder = make_builder(UVBuild)
        builder.name = "funx"
        builder.config_format(config, pkg_dir)
        return config

    def test_stale_setuptools_license_files_is_removed(self):
        with self.pkg({"LICENSE": "MIT\n"}) as d:
            config = self.apply({"project": {}, "tool": {"setuptools": {"license-files": []}}}, d)
        self.assertNotIn("license-files", config["tool"]["setuptools"])
        self.assertEqual(config["project"]["license-files"], ["LICENSE"])
        self.assertEqual(config["project"]["license"], "MIT")

    def test_legacy_license_table_becomes_spdx_string(self):
        with self.pkg({"LICENSE": "x\n"}) as d:
            config = self.apply({"project": {"license": {"text": "Apache-2.0"}}}, d)
        self.assertEqual(config["project"]["license"], "Apache-2.0")

    def test_existing_spdx_string_is_preserved(self):
        with self.pkg({"LICENSE": "x\n"}) as d:
            config = self.apply({"project": {"license": "BSD-3-Clause"}}, d)
        self.assertEqual(config["project"]["license"], "BSD-3-Clause")

    def test_missing_license_file_declares_nothing(self):
        """声明了 license-files 却找不到文件会让 setuptools 构建失败。"""
        with self.pkg({}) as d:
            config = self.apply({"project": {"license-files": ["LICENSE"]}}, d)
        self.assertNotIn("license-files", config["project"])

    def test_other_license_filenames_are_found(self):
        with self.pkg({"COPYING": "x\n"}) as d:
            config = self.apply({"project": {}}, d)
        self.assertEqual(config["project"]["license-files"], ["COPYING"])

    def test_setuptools_table_without_license_files_survives(self):
        with self.pkg({"LICENSE": "x\n"}) as d:
            config = self.apply({"project": {}, "tool": {"setuptools": {"packages": ["a"]}}}, d)
        self.assertEqual(config["tool"]["setuptools"], {"packages": ["a"]})


class WriteVersionEncodingTest(unittest.TestCase):
    """作者名是中文, 写文件必须显式 UTF-8, 否则非 UTF-8 locale 下会写坏清单。"""

    def test_pyproject_roundtrips_non_ascii(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pyproject.toml"
            path.write_text(
                '[project]\nname = "x"\nversion = "1.0.0"\ndescription = "中文描述"\n',
                encoding="utf-8",
            )
            builder = make_builder(UVBuild, version="1.0.1")
            builder.toml_paths = [str(path)]
            with patch("nltbuild.core.uv_build.sync_all_manifest_versions"):
                builder._write_version()
            reloaded = tomlkit.parse(path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["project"]["version"], "1.0.1")
        self.assertEqual(reloaded["project"]["description"], "中文描述")


class TomlFormattingPreservedTest(unittest.TestCase):
    """upgrade 只改版本号, 不得重写整个 pyproject.toml。

    旧实现用 toml.load/dump 往返, 会静默删掉全部注释、把多行数组压成一行、
    并重排 table 顺序 —— 每次发版都在 pyproject.toml 上留下满屏无关 diff。
    """

    SOURCE = """\
[build-system]
requires = ["setuptools>=77"]

[project]
name = "demo"
# 版本号由 nltbuild 维护, 不要手改
version = "1.0.0"
dependencies = [
    "requests>=2.0",  # HTTP 客户端
    "click",
]

[dependency-groups]
dev = ["pytest>=8"]

[tool.ruff]
line-length = 120
"""

    def upgrade_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pyproject.toml"
            path.write_text(self.SOURCE, encoding="utf-8")
            builder = make_builder(UVBuild, version="1.0.1")
            builder.toml_paths = [str(path)]
            with patch("nltbuild.core.uv_build.sync_all_manifest_versions"):
                builder._write_version()
            return path.read_text(encoding="utf-8")

    def test_only_the_version_line_changes(self):
        before = self.SOURCE.splitlines()
        after = self.upgrade_in_place().splitlines()
        differing = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(before), len(after), "行数不应变化")
        self.assertEqual(differing, [('version = "1.0.0"', 'version = "1.0.1"')])

    def test_comments_survive(self):
        after = self.upgrade_in_place()
        self.assertIn("# 版本号由 nltbuild 维护, 不要手改", after)
        self.assertIn("# HTTP 客户端", after)

    def test_multiline_array_is_not_collapsed(self):
        self.assertIn('    "requests>=2.0",  # HTTP 客户端\n', self.upgrade_in_place())

    def test_table_order_is_stable(self):
        def tables(text):
            return [line for line in text.splitlines() if line.startswith("[")]

        self.assertEqual(tables(self.upgrade_in_place()), tables(self.SOURCE))


class LazyBuilderTest(unittest.TestCase):
    """`--help` 不该触发仓库探测: 既慢, 又会让非 git 目录下连帮助都打不开。"""

    def run_cli(self, argv):
        with patch("nltbuild.core.cli.get_build") as get:
            with patch.object(sys, "argv", ["nltbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return get

    def test_help_does_not_detect_builder(self):
        self.run_cli(["--help"]).assert_not_called()

    def test_unknown_command_does_not_detect_builder(self):
        self.run_cli(["definitely-not-a-command"]).assert_not_called()

    def test_real_command_detects_builder_once(self):
        self.assertEqual(self.run_cli(["upgrade"]).call_count, 1)


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
