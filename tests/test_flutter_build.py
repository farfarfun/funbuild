"""FlutterBuild 的行为测试。"""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core.base import git_repo_root
from funbuild.core.flutter_build import FlutterBuild


def make_builder():
    """绕过 __init__ 里的 git 调用。"""
    builder = FlutterBuild.__new__(FlutterBuild)
    builder.repo_path = os.getcwd()
    builder.name = "repo"
    builder.version = None
    builder._build_number = None
    builder._funbuild_cfg = {}
    builder._pubspec_name = builder.name
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


PUBSPEC = """\
name: my_app
description: A sample app.
version: 1.2.3+7

environment:
  sdk: '>=3.0.0 <4.0.0'

dependencies:
  flutter:
    sdk: flutter

flutter:
  uses-material-design: true
"""

PUBSPEC_NO_BUILD_NUMBER = PUBSPEC.replace("version: 1.2.3+7", "version: 1.2.3")

PUBSPEC_PURE_DART = """\
name: my_pkg
version: 1.2.3

environment:
  sdk: '>=3.0.0 <4.0.0'
"""


class CheckTypeTest(unittest.TestCase):
    def test_valid_flutter_project_qualifies(self):
        with project({"pubspec.yaml": PUBSPEC}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "1.2.3")
            self.assertEqual(builder._build_number, 7)

    def test_missing_build_number_is_none(self):
        with project({"pubspec.yaml": PUBSPEC_NO_BUILD_NUMBER}):
            builder = make_builder()
            self.assertTrue(builder.check_type())
            self.assertIsNone(builder._build_number)

    def test_missing_pubspec_returns_false(self):
        with project({"README.md": "x"}):
            self.assertFalse(make_builder().check_type())

    def test_pure_dart_package_is_rejected(self):
        """没有 flutter SDK 依赖的纯 Dart 包不该被当成 Flutter 项目。"""
        with project({"pubspec.yaml": PUBSPEC_PURE_DART}):
            self.assertFalse(make_builder().check_type())

    def test_malformed_yaml_is_skipped(self):
        with project({"pubspec.yaml": "name: [unclosed"}):
            self.assertFalse(make_builder().check_type())

    def test_funbuild_config_is_captured(self):
        pubspec = PUBSPEC + "\nfunbuild:\n  build: make mobile\n"
        with project({"pubspec.yaml": pubspec}):
            builder = make_builder()
            builder.check_type()
            self.assertEqual(builder._funbuild_cfg, {"build": "make mobile"})


class CommandTest(unittest.TestCase):
    def builder_for(self, pubspec):
        with project({"pubspec.yaml": pubspec}):
            builder = make_builder()
            builder.check_type()
            return builder

    def test_default_build_targets_apk_and_web(self):
        builder = self.builder_for(PUBSPEC)
        self.assertEqual(
            builder._cmd_build(),
            ["flutter pub get", "flutter build apk --release", "flutter build web --release"],
        )

    def test_custom_build_string_overrides(self):
        pubspec = PUBSPEC + "\nfunbuild:\n  build: flutter build ios --release\n"
        builder = self.builder_for(pubspec)
        self.assertEqual(builder._cmd_build(), ["flutter pub get", "flutter build ios --release"])

    def test_custom_build_list_overrides(self):
        pubspec = PUBSPEC + "\nfunbuild:\n  build:\n    - flutter build apk\n    - flutter build appbundle\n"
        builder = self.builder_for(pubspec)
        self.assertEqual(builder._cmd_build(), ["flutter pub get", "flutter build apk", "flutter build appbundle"])

    def test_default_publish_uploads_apk_and_zipped_web_via_funpub(self):
        builder = self.builder_for(PUBSPEC)
        self.assertEqual(
            builder._cmd_publish(),
            [
                "(cd build/web && zip -rq ../web-release.zip .)",
                "funpub upload build/app/outputs/flutter-apk/app-release.apk flutter/my_app/apk "
                "--version 1.2.3 --repo-name funpackage",
                "funpub upload build/web-release.zip flutter/my_app/web --version 1.2.3 --repo-name funpackage",
            ],
        )

    def test_default_publish_uses_pubspec_name_not_repo_dirname(self):
        pubspec = PUBSPEC.replace("name: my_app", "name: other_pkg_name")
        builder = self.builder_for(pubspec)
        for cmd in builder._cmd_publish():
            self.assertNotIn("my_app", cmd)
        self.assertTrue(any("flutter/other_pkg_name/apk" in c for c in builder._cmd_publish()))
        self.assertTrue(any("flutter/other_pkg_name/web" in c for c in builder._cmd_publish()))

    def test_custom_publish_is_used(self):
        pubspec = PUBSPEC + "\nfunbuild:\n  publish: curl -T app.apk https://packages.example.com/app.apk\n"
        builder = self.builder_for(pubspec)
        self.assertEqual(builder._cmd_publish(), ["curl -T app.apk https://packages.example.com/app.apk"])

    def test_default_install_is_empty(self):
        builder = self.builder_for(PUBSPEC)
        self.assertEqual(builder._cmd_install(), [])

    def test_default_clean_runs_flutter_clean(self):
        builder = self.builder_for(PUBSPEC)
        self.assertEqual(builder._cmd_delete(), ["flutter clean"])

    def test_custom_clean_dirs(self):
        pubspec = PUBSPEC + "\nfunbuild:\n  cleanDirs:\n    - build\n    - .dart_tool\n"
        builder = self.builder_for(pubspec)
        self.assertEqual(builder._cmd_delete(), ["rm -rf build", "rm -rf .dart_tool"])

    def test_fvm_sdk_prefixes_build_and_delete_with_export_path(self):
        """存在 fvm 生成的 .fvm/flutter_sdk 时, build/delete 命令链最前面应插入
        一条 export PATH, 让链上后续 flutter/dart 命令解析到 fvm 锁定的版本。"""
        with project({"pubspec.yaml": PUBSPEC}) as root:
            fvm_bin = root / ".fvm" / "flutter_sdk" / "bin"
            fvm_bin.mkdir(parents=True)
            builder = make_builder()
            builder.check_type()
            export_cmd = f'export PATH={fvm_bin}:"$PATH"'
            self.assertEqual(
                builder._cmd_build(),
                [export_cmd, "flutter pub get", "flutter build apk --release", "flutter build web --release"],
            )
            self.assertEqual(builder._cmd_delete(), [export_cmd, "flutter clean"])

    def test_fvm_sdk_prefix_also_applies_to_custom_commands(self):
        with project({"pubspec.yaml": PUBSPEC}) as root:
            fvm_bin = root / ".fvm" / "flutter_sdk" / "bin"
            fvm_bin.mkdir(parents=True)
            pubspec_extra = "\nfunbuild:\n  cleanDirs:\n    - build\n"
            (root / "pubspec.yaml").write_text(PUBSPEC + pubspec_extra, encoding="utf-8")
            builder = make_builder()
            builder.check_type()
            export_cmd = f'export PATH={fvm_bin}:"$PATH"'
            self.assertEqual(builder._cmd_delete(), [export_cmd, "rm -rf build"])

    def test_without_fvm_sdk_no_prefix_is_added(self):
        """没有 .fvm/flutter_sdk 时命令链不受影响, 与改动前完全一致。"""
        builder = self.builder_for(PUBSPEC)
        self.assertNotIn(True, [cmd.startswith("export PATH=") for cmd in builder._cmd_build()])
        self.assertNotIn(True, [cmd.startswith("export PATH=") for cmd in builder._cmd_delete()])


class WriteVersionTest(unittest.TestCase):
    def test_build_number_is_incremented_and_main_version_synced(self):
        with project({"pubspec.yaml": PUBSPEC}) as root:
            builder = make_builder()
            builder.check_type()
            builder.version = "1.2.4"
            with patch("funbuild.core.flutter_build.sync_all_manifest_versions"):
                builder._write_version()
            text = (root / "pubspec.yaml").read_text(encoding="utf-8")
            self.assertIn("version: 1.2.4+8", text)
            self.assertEqual(builder._build_number, 8)

    def test_missing_build_number_stays_absent(self):
        with project({"pubspec.yaml": PUBSPEC_NO_BUILD_NUMBER}) as root:
            builder = make_builder()
            builder.check_type()
            builder.version = "1.2.4"
            with patch("funbuild.core.flutter_build.sync_all_manifest_versions"):
                builder._write_version()
            text = (root / "pubspec.yaml").read_text(encoding="utf-8")
            self.assertIn("version: 1.2.4\n", text)

    def test_rest_of_file_is_preserved(self):
        with project({"pubspec.yaml": PUBSPEC}) as root:
            builder = make_builder()
            builder.check_type()
            builder.version = "1.2.4"
            with patch("funbuild.core.flutter_build.sync_all_manifest_versions"):
                builder._write_version()
            text = (root / "pubspec.yaml").read_text(encoding="utf-8")
            self.assertIn("uses-material-design: true", text)
            self.assertIn("description: A sample app.", text)


if __name__ == "__main__":
    unittest.main()
