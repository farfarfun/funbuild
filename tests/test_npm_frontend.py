"""NpmFrontendBuild 的行为测试。

该模块此前零覆盖, 这里先把探测与命令生成这两条主路径钉住。
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core.base import git_repo_root
from funbuild.core.npm_frontend import NpmFrontendBuild


def make_builder():
    """绕过 __init__ 里的 git 调用。"""
    builder = NpmFrontendBuild.__new__(NpmFrontendBuild)
    builder.repo_path = os.getcwd()
    builder.name = "repo"
    builder.version = None
    builder.package_json_paths = []
    builder._pkg = {}
    builder._pm = "npm"
    builder._funbuild_cfg = {}
    return builder


@contextlib.contextmanager
def project(files):
    """在临时目录里铺出一个项目并 chdir 进去 (探测全部走相对路径)。"""
    with tempfile.TemporaryDirectory() as temp:
        for name, content in files.items():
            path = Path(temp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(temp)
        git_repo_root.cache_clear()
        try:
            yield Path(temp)
        finally:
            os.chdir(cwd)
            git_repo_root.cache_clear()


PKG = {"name": "web", "version": "1.0.0", "scripts": {"build": "vite build"}}


class CheckTypeTest(unittest.TestCase):
    def test_package_json_with_build_script_qualifies(self):
        with project({"package.json": PKG}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "1.0.0")

    def test_package_json_without_build_script_is_ignored(self):
        with project({"package.json": {"name": "web", "version": "1.0.0"}}):
            self.assertFalse(make_builder().check_type())

    def test_missing_package_json_returns_false(self):
        with project({"README.md": "x"}):
            self.assertFalse(make_builder().check_type())

    def test_root_pyproject_version_wins(self):
        """根 pyproject 是整仓版本主源, 优先于 package.json 自带版本。"""
        manifest = '[project]\nname = "x"\nversion = "2.5.0"\n'
        with project({"package.json": PKG, "pyproject.toml": manifest}):
            builder = make_builder()
            builder.check_type()
            self.assertEqual(builder.version, "2.5.0")

    def test_primary_state_is_populated(self):
        """check_type 必须把 _pkg/_pm/_funbuild_cfg 一并填好, 后续命令生成依赖它们。"""
        manifest = '[project]\nname = "x"\nversion = "2.5.0"\n'
        pkg = dict(PKG, funbuild={"packageManager": "pnpm"})
        with project({"package.json": pkg, "pyproject.toml": manifest}):
            builder = make_builder()
            builder.check_type()
            self.assertEqual(builder._pm, "pnpm")
            self.assertEqual(builder._pkg["name"], "web")
            self.assertEqual(builder._funbuild_cfg, {"packageManager": "pnpm"})

    def test_legacy_nltbuild_config_is_supported(self):
        self.assertEqual(
            NpmFrontendBuild._funbuild_from_pkg({"nltbuild": {"build": "make web"}}),
            {"build": "make web"},
        )

    def test_malformed_package_json_is_skipped(self):
        with project({"package.json": "{ not json"}):
            self.assertFalse(make_builder().check_type())

    def test_extbuild_subpackages_are_collected(self):
        with project({"extbuild/a/package.json": PKG, "extbuild/b/package.json": PKG}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(len(builder.package_json_paths), 2)


class PackageManagerDetectionTest(unittest.TestCase):
    def detect(self, files, cfg=None):
        with project(files) as root:
            return make_builder()._detect_package_manager_for_dir(str(root), cfg or {})

    def test_explicit_config_wins(self):
        self.assertEqual(self.detect({"pnpm-lock.yaml": ""}, {"packageManager": "yarn"}), "yarn")

    def test_versioned_config_value_is_parsed(self):
        self.assertEqual(self.detect({}, {"packageManager": "pnpm@9.1.0"}), "pnpm")

    def test_pnpm_lockfile(self):
        self.assertEqual(self.detect({"pnpm-lock.yaml": ""}), "pnpm")

    def test_yarn_lockfile(self):
        self.assertEqual(self.detect({"yarn.lock": ""}), "yarn")

    def test_defaults_to_npm(self):
        self.assertEqual(self.detect({}), "npm")


class CommandTest(unittest.TestCase):
    def builder_for(self, files):
        with project(files):
            builder = make_builder()
            builder.check_type()
            return builder, builder._cmd_build(), builder._cmd_publish()

    def test_npm_ci_used_when_lockfile_present(self):
        _, build, _ = self.builder_for({"package.json": PKG, "package-lock.json": "{}"})
        self.assertIn("npm ci", build)
        self.assertIn("npm run build", build)

    def test_npm_install_without_lockfile(self):
        _, build, _ = self.builder_for({"package.json": PKG})
        self.assertIn("npm install", build)

    def test_custom_build_command_overrides(self):
        pkg = dict(PKG, funbuild={"build": "make web"})
        _, build, _ = self.builder_for({"package.json": pkg})
        self.assertIn("make web", build)

    def test_private_package_is_not_published(self):
        pkg = dict(PKG, private=True)
        _, _, publish = self.builder_for({"package.json": pkg})
        self.assertEqual(publish, [])

    def test_publish_false_disables_publish(self):
        pkg = dict(PKG, funbuild={"publish": False})
        _, _, publish = self.builder_for({"package.json": pkg})
        self.assertEqual(publish, [])

    def test_default_publish_command(self):
        _, _, publish = self.builder_for({"package.json": PKG})
        self.assertEqual(publish, ["npm publish"])

    def test_subdirectory_commands_run_in_subshell(self):
        """用子 shell 隔离, 否则 && 串联时 cd 会残留并让后续相对路径错位。"""
        _, build, _ = self.builder_for({"extbuild/a/package.json": PKG})
        self.assertTrue(all(cmd.startswith("(cd extbuild/a && ") for cmd in build), build)


class WriteVersionTest(unittest.TestCase):
    def test_version_written_to_all_package_json(self):
        files = {"package.json": PKG, "extbuild/a/package.json": PKG}
        with project(files) as root:
            builder = make_builder()
            builder.check_type()
            builder.version = "3.1.4"
            with patch("funbuild.core.npm_frontend.sync_all_manifest_versions"):
                builder._write_version()
            for rel in ("package.json", "extbuild/a/package.json"):
                data = json.loads((root / rel).read_text(encoding="utf-8"))
                self.assertEqual(data["version"], "3.1.4", rel)

    def test_non_ascii_survives_write(self):
        pkg = dict(PKG, description="中文描述")
        with project({"package.json": pkg}) as root:
            builder = make_builder()
            builder.check_type()
            builder.version = "3.1.4"
            with patch("funbuild.core.npm_frontend.sync_all_manifest_versions"):
                builder._write_version()
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["description"], "中文描述")


if __name__ == "__main__":
    unittest.main()
