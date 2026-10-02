import unittest
from unittest.mock import patch

from funbuild.core.version_sync import ManifestVersionSyncError, sync_all_manifest_versions


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
            patch(f"funbuild.core.version_sync.{name}", return_value=paths)
            for name, paths in empty_collectors.items()
        ]
        with patches[0], patches[1], patches[2], patch(
            f"funbuild.core.version_sync.{writer}", side_effect=error
        ):
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


if __name__ == "__main__":
    unittest.main()
