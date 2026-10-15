"""CargoBuild (纯 Rust 仓库) 的行为测试。

这里**不执行任何 cargo 命令**: 所有 `_cmd_*` 方法只返回命令字符串列表, 真正执行
它们的是 `BaseBuild.build()` 里的 `run_checked`。所以测试直接断言返回的字符串,
锁文件那条路径则把 `run_checked` patch 掉再看调用参数。
"""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core import registry
from funbuild.core.base import git_repo_root
from funbuild.core.cargo_build import CargoBuild
from funbuild.core.maturin_build import MaturinBuild
from funbuild.core.registry import get_build
from funbuild.core.util import BuilderDetectionError, ManifestParseError, ShellCommandError
from funbuild.core.version_sync import ManifestVersionSyncError

# fundeploy 的形状: 单 crate、有注释、依赖表里也有 version 约束、带 release profile。
CARGO_BIN = """\
[package]
name = "demo"
version = "0.3.6"
edition = "2021"
# 这行注释有说明价值, 不许被格式化器重排掉
rust-version = "1.80"

[[bin]]
name = "demo"
path = "src/main.rs"

[dependencies]
clap = { version = "4", features = ["derive"] }
anyhow = "1"

[profile.release]
lto = true
codegen-units = 1
"""

# `version.workspace = true` + 字面量版本在 [workspace.package]。
CARGO_WORKSPACE_INHERIT = """\
[workspace]
members = ["crates/*"]

[workspace.package]
version = "1.4.2"

[package]
name = "demo"
version.workspace = true
"""

# 两处都有字面量: [package] 优先 (那才是 cargo publish 上传的版本)。
CARGO_BOTH_LITERAL = """\
[workspace.package]
version = "9.9.9"

[package]
name = "demo"
version = "1.0.0"
"""

# 纯虚拟 workspace: 根本没有包版本。
CARGO_VIRTUAL = """\
[workspace]
members = ["crates/*"]
"""

CARGO_BROKEN = """\
[package]
name = "demo
version = "1.0.0"
"""

# version 在一个谁都不该改的表下 —— 替换会落空。
CARGO_VERSION_IN_WRONG_SECTION = """\
[package]
name = "demo"

[dependencies]
version = "1.0.0"
"""

MATURIN_PYPROJECT = """\
[build-system]
requires = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "demo"
version = "0.3.6"
"""

PLAIN_PYPROJECT = """\
[project]
name = "demo"
version = "0.3.6"
"""


def make_builder(version="0.3.7"):
    """绕过 `__init__` 里的 git 调用, 手工装好字段。"""
    builder = CargoBuild.__new__(CargoBuild)
    builder.repo_path = os.getcwd()
    builder.name = "demo"
    builder.version = version
    return builder


@contextlib.contextmanager
def project(files):
    """在临时目录里铺出一个项目并 chdir 进去 (探测全部走相对路径)。"""
    with tempfile.TemporaryDirectory() as temp:
        for name, content in files.items():
            path = Path(temp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(temp)
        git_repo_root.cache_clear()
        try:
            yield Path(temp)
        finally:
            os.chdir(cwd)
            git_repo_root.cache_clear()


@contextlib.contextmanager
def repo(files):
    """同上, 但让 `git_repo_root` 也认这个临时目录, 以便跑整条 registry 探测。"""
    with project(files) as root:
        with patch("funbuild.core.base.run_shell", return_value=str(root)):
            git_repo_root.cache_clear()
            try:
                yield root
            finally:
                git_repo_root.cache_clear()


class CheckTypeTest(unittest.TestCase):
    def test_claims_a_pure_rust_repo(self):
        with project({"Cargo.toml": CARGO_BIN}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            # check_type 是双职责: 判定 + 把版本读出来。不读的话 tags() 会直接跳过。
            self.assertEqual(builder.version, "0.3.6")

    def test_reads_the_version_inherited_from_the_workspace_table(self):
        with project({"Cargo.toml": CARGO_WORKSPACE_INHERIT}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "1.4.2")

    def test_package_version_wins_over_workspace_package(self):
        """[package] 的版本才是 cargo publish 上传、release.yml 比对 tag 的那个。"""
        with project({"Cargo.toml": CARGO_BOTH_LITERAL}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "1.0.0")

    def test_does_not_claim_a_maturin_repo(self):
        with project({"Cargo.toml": CARGO_BIN, "pyproject.toml": MATURIN_PYPROJECT}):
            self.assertFalse(make_builder().check_type())
            # 前提: 这个仓库确实该由 MaturinBuild 认领, 否则上面那条断言是空的。
            maturin = MaturinBuild.__new__(MaturinBuild)
            maturin.repo_path = os.getcwd()
            maturin.name = "demo"
            maturin.version = None
            maturin.toml_paths = ["./pyproject.toml"]
            self.assertTrue(maturin.check_type())

    def test_does_not_claim_a_python_repo_with_a_vendored_crate(self):
        with project({"Cargo.toml": CARGO_BIN, "pyproject.toml": PLAIN_PYPROJECT}):
            self.assertFalse(make_builder().check_type())

    def test_does_not_claim_when_package_json_exists(self):
        with project({"Cargo.toml": CARGO_BIN, "package.json": '{"version": "1.0.0"}'}):
            self.assertFalse(make_builder().check_type())

    def test_ignores_a_repo_without_cargo_toml(self):
        with project({"README.md": "# demo\n"}):
            self.assertFalse(make_builder().check_type())

    def test_does_not_claim_a_virtual_workspace_without_version(self):
        """认了只会打出一个不对应任何 crate 的 tag。"""
        with project({"Cargo.toml": CARGO_VIRTUAL}):
            with patch("funbuild.core.cargo_build.logger") as log:
                self.assertFalse(make_builder().check_type())
            log.warning.assert_called_once()

    def test_cargo_toml_wins_over_a_stale_version_file(self):
        with project({"Cargo.toml": CARGO_BIN, "VERSION": "9.9.9\n"}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "0.3.6")

    def test_broken_manifest_raises_instead_of_silently_skipping(self):
        """返回 False 等于退化成 EmptyBuild + 退出码 0 —— 看起来像发布成功了。"""
        with project({"Cargo.toml": CARGO_BROKEN}):
            with self.assertRaises(ManifestParseError):
                make_builder().check_type()


class RegistryTest(unittest.TestCase):
    def test_registered_between_npm_frontend_and_version_file(self):
        """排在所有 Python / 前端类型之后是第二道保险, 在 VersionFileBuild 之前是因为
        Rust 仓库里的 VERSION 文件只会是过期副本。"""
        source = Path(registry.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index("NpmFrontendBuild,"), source.index("CargoBuild,"))
        self.assertLess(source.index("CargoBuild,"), source.index("VersionFileBuild,"))

    def test_a_pure_rust_repo_resolves_to_cargo_build(self):
        with repo({"Cargo.toml": CARGO_BIN}):
            builder = get_build()
            self.assertIsInstance(builder, CargoBuild)
            self.assertEqual(builder.version, "0.3.6")

    def test_broken_cargo_toml_refuses_to_degrade_to_empty_build(self):
        with repo({"Cargo.toml": CARGO_BROKEN}):
            with self.assertRaises(BuilderDetectionError) as cm:
                get_build()
            self.assertIn("CargoBuild", str(cm.exception))

    def test_empty_build_warning_mentions_cargo_toml(self):
        source = Path(registry.__file__).parent.joinpath("empty_build.py").read_text(encoding="utf-8")
        self.assertIn("Cargo.toml", source)


class CommandTest(unittest.TestCase):
    def _chain(self, builder):
        """`build()` 里实际串起来执行的那条链。"""
        return "\n".join(
            builder._cmd_delete()
            + builder._cmd_build()
            + builder._cmd_install()
            + builder._cmd_publish()
            + builder._cmd_delete()
        )

    def test_the_whole_release_chain_never_publishes(self):
        """最要紧的一条: crates.io 上的版本撤不回来。"""
        with project({"Cargo.toml": CARGO_BIN}):
            chain = self._chain(make_builder())
        self.assertNotIn("cargo publish", chain)
        self.assertNotIn("crates.io", chain)
        self.assertNotIn("cargo install", chain)

    def test_never_publishes_locally(self):
        with project({"Cargo.toml": CARGO_BIN}):
            self.assertEqual(make_builder()._cmd_publish(), [])

    def test_runs_the_three_ci_checks(self):
        with project({"Cargo.toml": CARGO_BIN}):
            self.assertEqual(
                make_builder()._cmd_build(),
                [
                    "cargo fmt --all --check",
                    "cargo clippy --all-targets -- -D warnings",
                    "cargo test",
                ],
            )

    def test_never_auto_formats_even_in_an_org_repo(self):
        """本地自动改完再提交等于让这道关卡失去意义。"""
        with project({"Cargo.toml": CARGO_BIN}):
            with patch.object(CargoBuild, "is_org_repo", property(lambda self: True)):
                commands = make_builder()._cmd_build()
        self.assertIn("cargo fmt --all --check", commands)
        self.assertNotIn("cargo fmt --all", commands)
        self.assertNotIn("uvx ruff format", "\n".join(commands))

    def test_install_is_not_pip_install(self):
        """基类默认是 pip install dist/*.whl, 在 Rust 仓库里必然失败。"""
        with project({"Cargo.toml": CARGO_BIN}):
            builder = make_builder()
            self.assertEqual(builder._cmd_install(), [])
            self.assertNotIn("pip", self._chain(builder))

    def test_delete_never_touches_cargo_lock_or_target(self):
        """publish 之后还会再跑一次 _cmd_delete, 紧接着就是 push。"""
        with project({"Cargo.toml": CARGO_BIN}):
            builder = make_builder()
            self.assertEqual(builder._cmd_delete(), [])
            chain = self._chain(builder)
        self.assertNotIn("Cargo.lock", chain)
        self.assertNotIn("rm -rf", chain)
        self.assertNotIn("cargo clean", chain)
        # 只找「清掉 target 目录」那种写法; `--all-targets` 是 clippy 的参数, 不算。
        self.assertNotIn("target/", chain)

    def test_published_distributions_is_empty(self):
        """让 _install_into_running_env 变成空操作。"""
        with project({"Cargo.toml": CARGO_BIN}):
            self.assertEqual(make_builder()._published_distributions(), {})


class WriteVersionTest(unittest.TestCase):
    def test_writes_the_package_version_only(self):
        # 没有 Cargo.lock, 所以锁文件那步自己跳过 —— 不需要 patch, 也不会碰 cargo。
        with project({"Cargo.toml": CARGO_BIN}) as root:
            make_builder("0.3.7")._write_version()
            cargo = (root / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn('version = "0.3.7"', cargo)
        self.assertNotIn('version = "0.3.6"', cargo)
        # 依赖约束、profile、注释一个都不许动。
        self.assertIn('clap = { version = "4", features = ["derive"] }', cargo)
        self.assertIn('anyhow = "1"', cargo)
        self.assertIn("[profile.release]", cargo)
        self.assertIn("# 这行注释有说明价值", cargo)
        self.assertIn('rust-version = "1.80"', cargo)

    def test_writes_the_workspace_package_version_when_inherited(self):
        with project({"Cargo.toml": CARGO_WORKSPACE_INHERIT}) as root:
            make_builder("2.0.0")._write_version()
            cargo = (root / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn('version = "2.0.0"', cargo)
        # 继承声明本身不是字面量, 必须原样留着。
        self.assertIn("version.workspace = true", cargo)

    def test_aborts_when_the_version_line_cannot_be_written(self):
        """写不进去却继续走, 后果是 tag 推出去之后 release.yml 的核对 job 才失败。"""
        with project({"Cargo.toml": CARGO_VERSION_IN_WRONG_SECTION}):
            with self.assertRaises(ManifestVersionSyncError):
                make_builder("0.3.7")._write_version()


class CargoLockTest(unittest.TestCase):
    def test_updates_the_lock_with_a_workspace_scoped_offline_update(self):
        with project({"Cargo.toml": CARGO_BIN, "Cargo.lock": "version = 4\n"}) as root:
            builder = make_builder("0.3.7")
            builder.repo_path = str(root)
            with patch("funbuild.core.cargo_build.run_checked") as run:
                builder._write_version()
        run.assert_called_once_with(["cargo update --workspace --offline"], cwd=str(root))
        # 不带 --workspace 的裸 cargo update 会把所有依赖升到最新兼容版。
        self.assertIn("--workspace", run.call_args[0][0][0])
        self.assertNotIn(" -p ", run.call_args[0][0][0])

    def test_skips_the_lock_update_when_the_repo_has_no_lock_file(self):
        """别造一个仓库本来不跟踪的文件: push 的 git add -A -f 会把它提交上去。"""
        with project({"Cargo.toml": CARGO_BIN}):
            with patch("funbuild.core.cargo_build.run_checked") as run:
                make_builder("0.3.7")._write_version()
        run.assert_not_called()

    def test_falls_back_to_an_online_workspace_update(self):
        with project({"Cargo.toml": CARGO_BIN, "Cargo.lock": "version = 4\n"}) as root:
            builder = make_builder("0.3.7")
            builder.repo_path = str(root)
            with patch(
                "funbuild.core.cargo_build.run_checked",
                side_effect=[ShellCommandError("offline failed"), None],
            ) as run:
                builder._write_version()
        self.assertEqual(run.call_count, 2)
        # 兜底仍然带 --workspace, 所以只会补齐缺失条目, 不会改已有的钉版。
        self.assertEqual(run.call_args_list[1][0][0], ["cargo update --workspace"])

    def test_propagates_when_both_lock_updates_fail(self):
        with project({"Cargo.toml": CARGO_BIN, "Cargo.lock": "version = 4\n"}) as root:
            builder = make_builder("0.3.7")
            builder.repo_path = str(root)
            with patch(
                "funbuild.core.cargo_build.run_checked",
                side_effect=ShellCommandError("boom"),
            ):
                with self.assertRaises(ShellCommandError):
                    builder._write_version()


if __name__ == "__main__":
    unittest.main()
