#!/usr/bin/python3

import os

from .base import BaseBuild
from .version_sync import sync_all_manifest_versions


class PypiBuild(BaseBuild):
    """PyPI构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version_path = "./script/__version__.md"

    def check_type(self):
        """检查是否为PyPI项目"""
        if not os.path.exists(self.version_path):
            return False
        with open(self.version_path, encoding="utf-8") as f:
            self.version = f.read().strip()
        return True

    def _write_version(self):
        """写入版本号到文件"""
        with open(self.version_path, "w", encoding="utf-8") as f:
            f.write(self.version)
        sync_all_manifest_versions(self.version)

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return []

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return [
            "pip install dist/*.whl",
        ]
