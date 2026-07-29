#!/usr/bin/python3

import os

from .base import BaseBuild
from .util import logger, parse_version
from .version_sync import sync_all_manifest_versions


class VersionFileBuild(BaseBuild):
    """以根目录纯文本 `VERSION` 文件为版本源的项目 (如 shell 脚本仓库)。

    这类仓库没有 pyproject.toml / package.json, 无从构建或发布, 但仍需要
    upgrade / push / tag 这套版本与 Git 流程。
    """

    VERSION_PATH = "./VERSION"

    def check_type(self) -> bool:
        if not os.path.isfile(self.VERSION_PATH):
            return False
        try:
            with open(self.VERSION_PATH, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError as e:
            logger.warning(f"skip VERSION file {self.VERSION_PATH}: {e}")
            return False
        if not raw:
            return False
        try:
            parse_version(raw)
        except ValueError:
            logger.warning(f"skip VERSION file, cannot parse content {raw!r}")
            return False
        # 去掉可能的 v 前缀: tags() 会自行拼 v, 否则会得到 vv0.1.7
        self.version = raw[1:] if raw.startswith("v") else raw
        return True

    def _write_version(self):
        with open(self.VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(f"{self.version}\n")
        sync_all_manifest_versions(self.version)

    def _cmd_build(self) -> list[str]:
        return []

    def _cmd_install(self) -> list[str]:
        return []

    def _cmd_publish(self) -> list[str]:
        return []

    def _cmd_delete(self) -> list[str]:
        return []
