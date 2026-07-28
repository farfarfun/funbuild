#!/usr/bin/python3

import os

import toml

from .base import BaseBuild
from .util import deep_get, logger
from .version_sync import sync_all_manifest_versions


class PoetryBuild(BaseBuild):
    """Poetry构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_path = "./pyproject.toml"

    def check_type(self) -> bool:
        """检查是否为 Poetry 项目: 必须存在 [tool.poetry].version。

        仅有 [tool.ruff] 之类其它 tool 段的 pyproject 不算 Poetry 项目, 此处
        必须返回 False 而不是抛 KeyError, 否则 registry 的探测链会整个中断。
        """
        if not os.path.exists(self.toml_path):
            return False
        try:
            a = toml.load(self.toml_path)
        except Exception as e:
            logger.warning(f"skip poetry check, cannot parse {self.toml_path}: {e}")
            return False
        version = deep_get(a, "tool", "poetry", "version")
        if not isinstance(version, str) or not version.strip():
            return False
        self.version = version.strip()
        return True

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
