#!/usr/bin/python3

import os

import toml

from .base import BaseBuild
from .version_sync import sync_all_manifest_versions


class PoetryBuild(BaseBuild):
    """Poetry构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_path = "./pyproject.toml"

    def check_type(self) -> bool:
        """检查是否为Poetry项目"""
        if os.path.exists(self.toml_path):
            a = toml.load(self.toml_path)
            if "tool" in a:
                self.version = a["tool"]["poetry"]["version"]
                return True
        return False

    def _write_version(self):
        """写入版本号到pyproject.toml"""
        a = toml.load(self.toml_path)
        a["tool"]["poetry"]["version"] = self.version
        with open(self.toml_path, "w") as f:
            toml.dump(a, f)
        sync_all_manifest_versions(self.version)

    def _cmd_publish(self) -> list[str]:
        """发布命令"""
        return ["poetry publish"]

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return ["poetry lock", "poetry build"]
