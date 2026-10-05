"""P0 修复的回归测试。

每个用例对应一个曾经可复现的失败, 用于防止回归。
"""

import contextlib
import importlib.metadata
import os
import shlex
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import tomlkit

from funbuild.core import util
from funbuild.core.base import BaseBuild, git_repo_root
from funbuild.core.cli import funbuild as cli_entry
from funbuild.core.empty_build import EmptyBuild
from funbuild.core.flutter_build import FlutterBuild
from funbuild.core.npm_frontend import NpmFrontendBuild
from funbuild.core.poetry_build import PoetryBuild
from funbuild.core.registry import get_build
from funbuild.core.util import (
    BuilderDetectionError,
    ManifestParseError,
    NotAGitRepositoryError,
    ShellCommandError,
    parse_version,
    run_checked,
)
from funbuild.core.uv_build import UVBuild
from funbuild.core.version_file_build import VersionFileBuild
from funbuild.tool.fastapi import ApiRoute, api_route


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


class UpgradeExplicitVersionTest(unittest.TestCase):
    """upgrade 传入显式版本号时应直接采用, 不再自动递增。"""

    def test_explicit_version_is_used_as_is(self):
        builder = make_builder(version="1.6.54")
        with patch.object(BaseBuild, "_write_version") as write_version:
            builder.upgrade(version="9.9.9")
        self.assertEqual(builder.version, "9.9.9")
        write_version.assert_called_once()

    def test_explicit_version_v_prefix_is_stripped(self):
        """否则 tags() 拼出来的 tag 会变成 vv9.9.9。"""
        builder = make_builder(version="1.6.54")
        with patch.object(BaseBuild, "_write_version"):
            builder.upgrade(version="v9.9.9")
        self.assertEqual(builder.version, "9.9.9")

    def test_no_version_keeps_auto_increment(self):
        builder = make_builder(version="1.6.54")
        with patch.object(BaseBuild, "_write_version"):
            builder.upgrade()
        self.assertEqual(builder.version, "1.6.55")

    def test_build_forwards_version_to_upgrade(self):
        builder = make_builder(version="1.0.0")
        with (
            patch.object(BaseBuild, "pull"),
            patch.object(BaseBuild, "_cmd_build", return_value=[]),
            patch.object(BaseBuild, "_cmd_delete", return_value=[]),
            patch.object(BaseBuild, "_cmd_install", return_value=[]),
            patch.object(BaseBuild, "_cmd_publish", return_value=[]),
            patch.object(BaseBuild, "_write_version"),
            patch.object(BaseBuild, "push"),
            patch.object(BaseBuild, "tags"),
        ):
            builder.build(version="3.1.4")
        self.assertEqual(builder.version, "3.1.4")


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
                with patch("funbuild.core.base.run_shell", return_value=temp):
                    builder = get_build()
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()
            self.assertIsNotNone(builder)

    @contextlib.contextmanager
    def repo(self, files):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in files.items():
                path = Path(temp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(temp)
            git_repo_root.cache_clear()
            try:
                with patch("funbuild.core.base.run_shell", return_value=temp):
                    yield Path(temp)
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

    def test_broken_builder_is_skipped_not_fatal(self):
        """某个 builder 探测出错不该连累后面本能命中的 builder。"""
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}):
            with patch.object(PoetryBuild, "check_type", side_effect=RuntimeError("boom")):
                builder = get_build()
        self.assertIsInstance(builder, UVBuild)
        self.assertEqual(builder.version, "1.0.0")

    def test_no_manifest_at_all_falls_back_to_empty_build(self):
        """真的没有任何清单 (纯文档仓库) 时, 兜底到 EmptyBuild 仍是正确行为。"""
        with self.repo({"README.md": "# docs\n"}):
            builder = get_build()
        self.assertIsInstance(builder, EmptyBuild)

    def test_corrupt_pyproject_aborts_instead_of_silent_empty_build(self):
        """pyproject.toml 写坏时必须报错中止。

        原先探测异常只记一条 warning, 最终落到 EmptyBuild —— `funbuild build`
        什么都不做却以退出码 0 结束, 看起来像发布成功了。
        """
        with self.repo({"pyproject.toml": '[project]\nname = "x\nversion = "1.0.0"\n'}):
            with self.assertRaises(BuilderDetectionError) as ctx:
                get_build()
        self.assertIn("UVBuild", str(ctx.exception))
        # 领域异常要带上出错的文件路径, 否则多清单仓库无从定位
        self.assertIn("pyproject.toml", str(ctx.exception))


class LoadTomlErrorContextTest(unittest.TestCase):
    """tomlkit 的解析异常不带文件名, extbuild/ 多清单仓库下无从定位。"""

    def test_parse_error_carries_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pyproject.toml"
            path.write_text('[project]\nname = "x\n', encoding="utf-8")
            with self.assertRaises(ManifestParseError) as ctx:
                util.load_toml(str(path))
        self.assertIn(str(path), str(ctx.exception))

    def test_missing_file_carries_path(self):
        with self.assertRaises(ManifestParseError) as ctx:
            util.load_toml("/nonexistent/dir/pyproject.toml")
        self.assertIn("/nonexistent/dir/pyproject.toml", str(ctx.exception))


class ApiRouteDeprecationTest(unittest.TestCase):
    """SPEC §15.1: 改名后旧接口要保留并发 DeprecationWarning。"""

    def test_old_name_warns_and_still_works(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            decorator = api_route("/ping", methods=["GET"])
        self.assertIsInstance(decorator, ApiRoute)
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, DeprecationWarning)
        self.assertIn("ApiRoute", str(caught[0].message))

    def test_new_name_does_not_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ApiRoute("/ping")
        self.assertEqual([w for w in caught if w.category is DeprecationWarning], [])


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
                with patch("funbuild.core.base.run_shell", return_value=temp):
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
        # farlog 走 loguru, 不经 stdlib logging, assertLogs 抓不到它的输出,
        # 因此直接断言 logger 被调用。
        with self.repo({"README.md": "x\n"}):
            with patch("funbuild.core.empty_build.logger") as log:
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
        with patch("funbuild.core.util.shutil.which", return_value=None) as which:
            with patch("funbuild.core.util.run_shell", return_value="1"):
                with patch("funbuild.core.util.run_checked") as run:
                    for _ in range(5):
                        self.assertFalse(util.aicommits_commit())

        self.assertEqual(which.call_count, 1, "aicommits 可用性只应探测一次")
        run.assert_not_called()

    def test_available_cli_is_invoked(self):
        # 依次是: 查暂存(有) / aicommits 输出 / 再查暂存(已提交) / 读回提交信息
        with patch("funbuild.core.util.shutil.which", return_value="/usr/bin/aicommits"):
            with patch(
                "funbuild.core.util.run_shell", side_effect=["1", "feat: 自动生成信息", "0", "feat: 自动生成信息"]
            ) as run_shell:
                with patch("funbuild.core.util.run_checked") as run:
                    self.assertTrue(util.aicommits_commit())
        self.assertIn(util.AICOMMITS_COMMAND, [c.args[0] for c in run_shell.call_args_list])
        run.assert_not_called()

    def test_conventional_format_is_requested(self):
        """aicommits 的默认 plain 模式只输出纯描述, 生成的信息 100% 缺 `<类型>:` 前缀;
        它的 conventional 模式又会产出表外的 perf/style/ci (实测如此)。信息既然是
        funbuild 让它生成的, 格式就得由 funbuild 交代, 不能生成完再告警。"""
        self.assertIn("--type conventional", util.AICOMMITS_COMMAND)
        for commit_type in util.COMMIT_MESSAGE_TYPES:
            self.assertIn(commit_type, util.AICOMMITS_COMMAND)
        self.assertNotIn("--locale", util.AICOMMITS_COMMAND, "描述不限语种, 语种听用户自己的配置")

    def test_generated_message_is_committed_when_aicommits_only_printed(self):
        """aicommits 4.x 的 `--yes` 只在 TTY 下才真的提交; 经 shell 调用时它把信息
        打到 stdout 就退出 (退出码 0, 暂存原地不动)。funbuild 必须接住这条信息自己
        提交 —— 否则标题全成了兜底的「更新项目文件」。"""
        with patch("funbuild.core.util.shutil.which", return_value="/usr/bin/aicommits"):
            with patch("funbuild.core.util.run_shell", side_effect=["1", "feat: 自动生成信息\n", "1"]):
                with patch("funbuild.core.util.run_checked") as run:
                    self.assertTrue(util.aicommits_commit())
        run.assert_called_once_with(["git commit -m 'feat: 自动生成信息'"], cwd=None)

    def test_unconventional_generated_message_is_still_used(self):
        """不合约定也照原样提交, 不退回兜底信息。"""
        with patch("funbuild.core.util.shutil.which", return_value="/usr/bin/aicommits"):
            with patch("funbuild.core.util.run_shell", side_effect=["1", "放开提交信息校验\n", "1"]):
                with patch("funbuild.core.util.run_checked") as run:
                    self.assertTrue(util.aicommits_commit())
        run.assert_called_once_with(["git commit -m '放开提交信息校验'"], cwd=None)


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

    def repair(self, generated, fallback=util.DEFAULT_COMMIT_MESSAGE):
        with patch("funbuild.core.util.run_shell", return_value=generated):
            with patch("funbuild.core.util.run_checked") as run:
                util._repair_generated_message("/repo", fallback)
        return run.call_args_list

    def test_think_only_message_is_amended_to_fallback(self):
        amends = self.repair("<think>\n")
        self.assertEqual(amends, [call(["git commit --amend -m 'chore: 更新项目文件'"], cwd="/repo")])

    def test_recoverable_message_is_amended_to_conclusion(self):
        amends = self.repair("<think>想了想</think>\nfix: 真正的信息")
        self.assertEqual(amends, [call(["git commit --amend -m 'fix: 真正的信息'"], cwd="/repo")])

    def test_clean_message_is_left_alone(self):
        self.assertEqual(self.repair("fix: 一条正常的信息\n"), [])

    def test_english_description_is_left_alone(self):
        """描述不限语种: aicommits 多数时候生成英文, 不该一律被换成回退信息。"""
        self.assertEqual(self.repair("fix: generated message"), [])

    def test_unconventional_message_is_not_rewritten(self):
        """只清污染, 不碰格式。

        funbuild 自己 1.6.84 到 1.6.92 的提交标题全是「更新项目文件」就是这么来的:
        aicommits 的 plain 模式只输出纯描述, 一判不合约定就整条换成回退信息, AI 写好
        的描述全丢了。类型表外的 perf/style/ci、中文类型词、纯描述, 现在一律原样保留。
        """
        for generated in (
            "修复: 这不是合规的类型",
            "提交信息校验不再要求描述含中文",
            "perf: 加快依赖解析",
            "放开提交信息校验\n\n正文照旧保留",
        ):
            with self.subTest(generated=generated):
                self.assertEqual(self.repair(generated), [])


class IsValidCommitMessageTest(unittest.TestCase):
    """回归: 曾把 SPEC §10 的 `<类型>: <做了什么>` 误读成「类型也必须是中文」,
    于是 `fix: 修复版本解析` —— 完全合规的信息 —— 被判非法, 全组织 push 被堵死。"""

    def test_spec_types_are_accepted(self):
        for commit_type in util.COMMIT_MESSAGE_TYPES:
            with self.subTest(commit_type=commit_type):
                self.assertTrue(util.is_valid_commit_message(f"{commit_type}: 修复版本解析"))

    def test_optional_scope_is_accepted(self):
        self.assertTrue(util.is_valid_commit_message("fix(core): 修复版本解析"))

    def test_body_after_subject_is_ignored(self):
        self.assertTrue(util.is_valid_commit_message("feat: 新增发布流程\n\n正文随便写, 不参与校验"))

    def test_default_fallback_message_is_itself_valid(self):
        self.assertTrue(util.is_valid_commit_message(util.DEFAULT_COMMIT_MESSAGE))

    def test_chinese_type_is_rejected(self):
        self.assertFalse(util.is_valid_commit_message("修复: 处理版本解析边界"))

    def test_unknown_type_is_rejected(self):
        self.assertFalse(util.is_valid_commit_message("build: 构建产物"))

    def test_english_description_is_accepted(self):
        """描述不限语种 —— 曾要求必须含中文, aicommits 的英文信息因此全被拒。"""
        self.assertTrue(util.is_valid_commit_message("fix: parse version"))

    def test_missing_type_is_rejected(self):
        self.assertFalse(util.is_valid_commit_message("修复了版本解析的边界问题"))

    def test_missing_space_after_colon_is_rejected(self):
        self.assertFalse(util.is_valid_commit_message("fix:修复版本解析"))

    def test_empty_and_none_are_rejected(self):
        self.assertFalse(util.is_valid_commit_message(""))
        self.assertFalse(util.is_valid_commit_message(None))
        self.assertFalse(util.is_valid_commit_message("fix: "))


class UpgradeCommandVersionOptionTest(unittest.TestCase):
    """upgrade 命令的 --version 需原样透传给 builder().upgrade。"""

    def invoke(self, argv):
        builder = MagicMock()
        with patch("funbuild.core.cli.get_build", return_value=builder):
            with patch.object(sys, "argv", ["funbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return builder

    def test_no_version_defaults_to_none(self):
        builder = self.invoke(["upgrade"])
        builder.upgrade.assert_called_once_with(version=None)

    def test_version_option_is_forwarded(self):
        builder = self.invoke(["upgrade", "--version", "2.0.0"])
        builder.upgrade.assert_called_once_with(version="2.0.0")


class ReleaseAliasTest(unittest.TestCase):
    """release 必须与 build 走同一条流水线。"""

    def invoke(self, argv):
        builder = MagicMock()
        with patch("funbuild.core.cli.get_build", return_value=builder):
            with patch.object(sys, "argv", ["funbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return builder

    def test_release_dispatches_to_build(self):
        builder = self.invoke(["release"])
        builder.build.assert_called_once_with(message=None, version=None)

    def test_release_accepts_positional_message(self):
        builder = self.invoke(["release", "chore: 发布一下"])
        builder.build.assert_called_once_with(message="chore: 发布一下", version=None)

    def test_build_accepts_version_option(self):
        builder = self.invoke(["build", "chore: 发布一下", "--version", "2.0.0"])
        builder.build.assert_called_once_with(message="chore: 发布一下", version="2.0.0")

    def test_invalid_message_is_passed_through(self):
        """不合约定只提醒一句就继续: 曾经是非 0 退出, 于是正常的信息一旦判错就既发
        不了版、也留不下描述。信息必须原样透传给 builder。"""
        for argv, command in ((["build", "随手写的信息"], "build"), (["release", "随手写的信息"], "build")):
            with self.subTest(argv=argv):
                builder = self.invoke(argv)
                getattr(builder, command).assert_called_once_with(message="随手写的信息", version=None)
        builder = self.invoke(["push", "-m", "随手写的信息"])
        builder.push.assert_called_once_with("随手写的信息", batch_size=20)

    def test_release_matches_build(self):
        self.assertEqual(
            self.invoke(["release", "chore: 同一条信息"]).build.call_args,
            self.invoke(["build", "chore: 同一条信息"]).build.call_args,
        )


class RepoRootCacheTest(unittest.TestCase):
    """registry 会实例化 8 个 builder (hybrid 再建 2 个), 每个都跑一次
    `git rev-parse`, 单次 CLI 调用因此有 9 次 subprocess。现按 cwd 缓存为 1 次。"""

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
            with patch("funbuild.core.base.run_shell", return_value=str(root)) as shell:
                get_build()
        self.assertEqual(shell.call_count, 1, f"git rev-parse 应只执行一次, 实际 {shell.call_count} 次")

    def test_repo_path_is_stripped(self):
        """未 strip 时 name 会带换行, 被拼进 project.urls 生成非法 URL。"""
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}) as root:
            with patch("funbuild.core.base.run_shell", return_value=f"{root}\n"):
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
            with patch("funbuild.core.base.run_shell", return_value=str(root)):
                builder = get_build()
                self.assertEqual(os.getcwd(), str(root), "应已归一化到仓库根")
        self.assertNotIsInstance(builder, EmptyBuild)
        self.assertEqual(builder.version, "2.0.0")

    def test_outside_git_repo_raises_clear_error(self):
        """原先是 subprocess 抛 FileNotFoundError: '', 完全看不出真正原因。"""
        with self.repo({"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.0"\n'}) as root:
            os.chdir(root)
            with patch("funbuild.core.base.run_shell", return_value=""):
                with self.assertRaises(NotAGitRepositoryError):
                    get_build()

    def test_git_root_pointing_nowhere_raises(self):
        with self.repo({}) as root:
            os.chdir(root)
            with patch("funbuild.core.base.run_shell", return_value="/nonexistent/repo"):
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
        builder.name = "otherpackage"
        config = {"project": {"name": "otherpackage"}}
        builder.config_format(config)
        self.assertEqual(config, {"project": {"name": "otherpackage"}})


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
            with patch("funbuild.core.uv_build.sync_all_manifest_versions"):
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
# 版本号由 funbuild 维护, 不要手改
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
            with patch("funbuild.core.uv_build.sync_all_manifest_versions"):
                builder._write_version()
            return path.read_text(encoding="utf-8")

    def test_only_the_version_line_changes(self):
        before = self.SOURCE.splitlines()
        after = self.upgrade_in_place().splitlines()
        differing = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
        self.assertEqual(len(before), len(after), "行数不应变化")
        self.assertEqual(differing, [('version = "1.0.0"', 'version = "1.0.1"')])

    def test_comments_survive(self):
        after = self.upgrade_in_place()
        self.assertIn("# 版本号由 funbuild 维护, 不要手改", after)
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
        with patch("funbuild.core.cli.get_build") as get:
            with patch.object(sys, "argv", ["funbuild", *argv]):
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
        with patch("funbuild.core.cli.get_build", return_value=builder):
            with patch.object(sys, "argv", ["funbuild", *argv]):
                with contextlib.suppress(SystemExit):
                    cli_entry()
        return builder

    def test_tag_dispatches_to_builder(self):
        self.invoke(["tag"]).tags.assert_called_once_with()

    def test_old_tags_name_is_gone(self):
        self.invoke(["tags"]).tags.assert_not_called()


class UVBuildNameTest(unittest.TestCase):
    """rename 场景: pyproject 已改名但目录还没同步, self.name 不该用目录名。"""

    def test_name_prefers_pyproject_over_stale_dir_name(self):
        with tempfile.TemporaryDirectory() as temp:
            # 目录本身模拟"还没改名"的旧路径
            old_dir = Path(temp) / "nltcache"
            old_dir.mkdir()
            (old_dir / "pyproject.toml").write_text('[project]\nname = "farcache"\n', encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(old_dir)
            git_repo_root.cache_clear()
            try:
                with patch("funbuild.core.base.run_shell", return_value=str(old_dir)):
                    builder = UVBuild()
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()
            self.assertEqual(builder.name, "farcache")

    def test_explicit_name_still_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            old_dir = Path(temp) / "nltcache"
            old_dir.mkdir()
            (old_dir / "pyproject.toml").write_text('[project]\nname = "farcache"\n', encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(old_dir)
            git_repo_root.cache_clear()
            try:
                with patch("funbuild.core.base.run_shell", return_value=str(old_dir)):
                    builder = UVBuild(name="explicit")
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()
            self.assertEqual(builder.name, "explicit")


class CleanupKeepsTrackedFilesTest(unittest.TestCase):
    """回归: `_cmd_delete` 里混进了 `rm -rf uv.lock`, 而 `build()` 在 publish 之后
    还会再跑一次 `_cmd_delete` 并紧接着 push —— 于是每次发版都把 SPEC §5 要求提交的
    uv.lock 从仓库里删掉一次 (funbuild 自己的 1.6.81 发版提交就是实例)。"""

    @contextlib.contextmanager
    def builder(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "pkg"
            repo.mkdir()
            (repo / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "1.0.0"\n', encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(repo)
            git_repo_root.cache_clear()
            try:
                with patch("funbuild.core.base.run_shell", return_value=str(repo)):
                    yield UVBuild()
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

    def test_cleanup_does_not_touch_uv_lock(self):
        with self.builder() as build:
            for command in build._cmd_delete():
                self.assertNotIn("uv.lock", command)

    def test_cleanup_only_removes_build_artifacts(self):
        with self.builder() as build:
            for command in build._cmd_delete():
                self.assertRegex(command, r"dist|build|egg-info")

    def test_build_still_refreshes_the_lock_first(self):
        """删 lock 重新解析的意图保留下来, 只是挪到构建之前。"""
        with self.builder() as build:
            self.assertEqual(build._cmd_build()[:2], ["rm -rf uv.lock", "uv lock --prerelease=allow"])


if __name__ == "__main__":
    unittest.main()


class CleanDirsSanitizeTest(unittest.TestCase):
    """`funbuild.cleanDirs` 来自 package.json / pubspec.yaml, 原先未经任何校验就
    f-string 拼进 `rm -rf {d}` 交给 shell。"""

    def safe(self, value):
        return util.safe_clean_dir(value, source="test")

    def test_relative_path_passes(self):
        self.assertEqual(self.safe("dist"), "dist")
        self.assertEqual(self.safe("  build/web  "), "build/web")

    def test_glob_still_allowed(self):
        """默认清理项本来就有 extbuild/*/dist, 不能把 glob 一并封掉。"""
        self.assertEqual(self.safe("extbuild/*/dist"), "extbuild/*/dist")

    def test_absolute_path_rejected(self):
        self.assertIsNone(self.safe("/"))
        self.assertIsNone(self.safe("/etc"))
        self.assertIsNone(self.safe("~/Documents"))

    def test_parent_traversal_rejected(self):
        self.assertIsNone(self.safe("../../etc"))
        self.assertIsNone(self.safe("build/../../.."))

    def test_shell_metacharacters_rejected(self):
        self.assertIsNone(self.safe("dist; rm -rf ~"))
        self.assertIsNone(self.safe("$(whoami)"))
        self.assertIsNone(self.safe("`id`"))
        self.assertIsNone(self.safe("dist && curl evil.sh"))

    def test_space_rejected(self):
        """带空格会被 shell 拆成两个删除目标, 删掉配置里没写的东西。"""
        self.assertIsNone(self.safe("my build"))

    def test_blank_and_non_string_rejected(self):
        self.assertIsNone(self.safe("   "))
        self.assertIsNone(self.safe(None))
        self.assertIsNone(self.safe(123))

    def test_npm_cmd_delete_filters_unsafe_entries(self):
        builder = make_builder(NpmFrontendBuild)
        builder._funbuild_cfg = {"cleanDirs": ["dist", "/etc", "x; rm -rf ~", "extbuild/*/out"]}
        self.assertEqual(builder._cmd_delete(), ["rm -rf dist", "rm -rf extbuild/*/out"])

    def test_flutter_cmd_delete_filters_unsafe_entries(self):
        builder = make_builder(FlutterBuild)
        builder._funbuild_cfg = {"cleanDirs": ["build", "../../"]}
        with patch.object(FlutterBuild, "_fvm_path_prefix", return_value=[]):
            self.assertEqual(builder._cmd_delete(), ["rm -rf build"])


class InstallIntoRunningEnvTest(unittest.TestCase):
    """发完版要把新版本装进「正在运行 funbuild 的那个环境」。

    `_cmd_install` 装的是项目的 .venv, 而命令往往装在别处 —— funbuild 自己的
    `funbuild` 在 py312, 连发 1.6.85/1.6.87/1.6.90/1.6.91 四版, py312 一直停在
    1.6.87, 每次改动都「看起来没生效」。
    """

    def run_install(self, *, installed, repo_path="/repo", status="0"):
        """installed: 本机已装的分发名 -> 版本; 不在表里的视为未安装。"""
        builder = make_builder(repo_path=repo_path)

        def version(dist):
            if dist in installed:
                return installed[dist]
            raise importlib.metadata.PackageNotFoundError(dist)

        with patch("funbuild.core.base.importlib.metadata.version", side_effect=version):
            with patch("funbuild.core.base.os.path.isdir", return_value=True):
                with patch("funbuild.core.base.run_shell", return_value=status) as run_shell:
                    builder._install_into_running_env()
        return [c.args[0] for c in run_shell.call_args_list]

    def test_already_installed_distribution_is_upgraded(self):
        commands = self.run_install(installed={"repo": "1.6.94"})
        self.assertEqual(commands, [shlex.join(["uv", "pip", "install", "--python", sys.executable, "/repo"])])

    def test_absent_distribution_is_left_alone(self):
        """发业务包时不该往命令所在的环境里塞一堆它并不需要的包。"""
        self.assertEqual(self.run_install(installed={}), [])

    def test_failure_is_warned_not_raised(self):
        """版本已发布、提交和 tag 都在, 唯一后果是本机没升级, 不该算发版失败。"""
        with patch("funbuild.core.base.logger.warning") as warning:
            self.run_install(installed={"repo": "1.6.94"}, status="1")
        self.assertTrue(any("手动重试" in str(c) for c in warning.call_args_list))

    def test_running_inside_project_venv_is_skipped(self):
        """跑在项目自己的 .venv 里时, 安装校验那一步装的就是这个环境。"""
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, ".venv"))
            with patch("funbuild.core.base.sys.prefix", os.path.join(temp, ".venv")):
                with patch("funbuild.core.base.run_shell") as run_shell:
                    make_builder(repo_path=temp)._install_into_running_env()
        run_shell.assert_not_called()

    def test_build_installs_after_tagging(self):
        """必须排在 push/tag 之后: 发 funbuild 时这一步在替换 funbuild 自己的
        site-packages, 出问题不能留下「已发布但没提交」的半截状态。"""
        builder = make_builder()
        order = []
        with patch.object(BaseBuild, "pull"), patch.object(BaseBuild, "upgrade"):
            with patch.object(BaseBuild, "_sync_latest_dependencies"):
                with patch("funbuild.core.base.run_checked"):
                    with patch.object(BaseBuild, "push", side_effect=lambda **kw: order.append("push")):
                        with patch.object(BaseBuild, "tags", side_effect=lambda: order.append("tag")):
                            with patch.object(
                                BaseBuild,
                                "_install_into_running_env",
                                side_effect=lambda: order.append("install"),
                            ):
                                builder.build(message="chore: 发版")
        self.assertEqual(order, ["push", "tag", "install"])
