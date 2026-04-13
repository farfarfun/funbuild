#!/usr/bin/python3

from .base import BaseBuild


class EmptyBuild(BaseBuild):
    """UV构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self) -> bool:
        """检查是否为UV项目"""
        return True

    def _write_version(self):
        pass

    def config_format(self, config):
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
