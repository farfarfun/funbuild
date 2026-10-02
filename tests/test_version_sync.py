import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core.version_sync import ManifestVersionSyncError, sync_all_manifest_versions


@contextlib.contextmanager
def chdir_tmp():
    """version_sync 全部按相对路径解析清单, 必须在临时目录里跑。"""
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as temp:
        os.chdir(temp)
        try:
            yield Path(temp)
        finally:
            os.chdir(original)


class ManifestVersionSyncFailureTest(unittest.TestCase):
    """清单写入失败必须终止同步，避免发布流程带着不一致版本继续。"""

    def assert_sync_failure(self, collector: str, writer: str, path: str, error: Exception) -> None:
        empty_collectors = {
            "_collect_package_json_paths_for_version_sync": [],
            "_collect_pyproject_paths_for_version_sync": [],
            "_collect_pubspec_paths_for_version_sync": [],
        }
        empty_collectors[collector] = [path]
        patches = [
            patch(f"funbuild.core.version_sync.{name}", return_value=paths) for name, paths in empty_collectors.items()
        ]
        with patches[0], patches[1], patches[2], patch(f"funbuild.core.version_sync.{writer}", side_effect=error):
            with self.assertRaisesRegex(ManifestVersionSyncError, path):
                sync_all_manifest_versions("1.2.3")

    def test_package_json_write_failure_is_fatal(self) -> None:
        self.assert_sync_failure(
            "_collect_package_json_paths_for_version_sync",
            "_sync_package_json_version_file",
            "exts/web/package.json",
            OSError("disk full"),
        )

    def test_pyproject_write_failure_is_fatal(self) -> None:
        self.assert_sync_failure(
            "_collect_pyproject_paths_for_version_sync",
            "_sync_pyproject_version_file",
            "extbuild/api/pyproject.toml",
            ValueError("invalid document"),
        )

    def test_pubspec_write_failure_is_fatal(self) -> None:
        self.assert_sync_failure(
            "_collect_pubspec_paths_for_version_sync",
            "_sync_pubspec_version_file",
            "pubspec.yaml",
            PermissionError("read only"),
        )


class UnparsableManifestTest(unittest.TestCase):
    """清单读不出来时不能静默少同步一个: 那会让发布带着不一致的版本号继续。"""

    def test_broken_pyproject_is_fatal(self) -> None:
        with chdir_tmp() as root:
            (root / "pyproject.toml").write_text("[project\nname = 这不是 toml\n", encoding="utf-8")
            with self.assertRaisesRegex(ManifestVersionSyncError, "pyproject.toml"):
                sync_all_manifest_versions("1.2.3")

    def test_broken_package_json_is_fatal(self) -> None:
        with chdir_tmp() as root:
            (root / "package.json").write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ManifestVersionSyncError, "package.json"):
                sync_all_manifest_versions("1.2.3")

    def test_pyproject_without_version_is_skipped_not_fatal(self) -> None:
        """能解析、只是没有 version 字段 (如只写 [tool.ruff]) 是正常情况。"""
        with chdir_tmp() as root:
            (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
            sync_all_manifest_versions("1.2.3")
            self.assertNotIn("1.2.3", (root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_repo_without_pyproject_is_not_fatal(self) -> None:
        """回归: 根 pyproject.toml 曾被无条件纳入待查清单, 配上「解析失败即中止」
        会让 VERSION 文件仓库 / 纯前端 / 纯 Flutter 仓库一律在版本同步时炸掉。"""
        with chdir_tmp() as root:
            (root / "package.json").write_text(json.dumps({"name": "web", "version": "0.0.1"}), encoding="utf-8")
            sync_all_manifest_versions("1.2.3")
            self.assertEqual(json.loads((root / "package.json").read_text(encoding="utf-8"))["version"], "1.2.3")

    def test_empty_repo_is_a_noop(self) -> None:
        with chdir_tmp():
            sync_all_manifest_versions("1.2.3")


if __name__ == "__main__":
    unittest.main()
