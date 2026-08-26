#!/usr/bin/python3

from .base import BaseBuild
from .util import logger


class EmptyBuild(BaseBuild):
    """兜底构建类: 所有命令均为空操作。

    永远匹配, 因此必须排在注册表最后。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self) -> bool:
        """兜底匹配。走到这里说明没有任何构建策略认领本仓库。"""
        logger.warning(
            "未识别到版本清单 (pyproject.toml / package.json / VERSION / script/__version__.md), "
            "回退到 EmptyBuild: upgrade、tag、build 将不会有任何效果"
        )
        return True

    def _write_version(self):
        pass

    def config_format(self, config, pkg_dir="."):
        pass

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return []

    def _cmd_publish(self) -> list[str]:
        return []

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return []

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return []
