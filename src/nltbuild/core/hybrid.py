#!/usr/bin/python3

from typing import Optional

from funshell import run_shell_list

from .base import BaseBuild
from .npm_frontend import NpmFrontendBuild
from .util import logger
from .uv_build import UVBuild


class UvNpmHybridBuild(BaseBuild):
    """根目录为 UV ([project]) 的 Python 包, 且存在可构建的前端 package.json 时, 一条命令串联 UV 与前端构建/安装/发布."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._uv: Optional[UVBuild] = None
        self._npm: Optional[NpmFrontendBuild] = None

    def check_type(self) -> bool:
        uv = UVBuild()
        if not uv.check_type():
            return False
        npm = NpmFrontendBuild()
        if not npm.check_type():
            return False
        self._uv = uv
        self._npm = npm
        self.version = uv.version
        logger.info("detected hybrid repo: UV Python + npm/pnpm/yarn frontend, single build runs both")
        return True

    def _write_version(self):
        if self._uv is None:
            return
        self._uv.version = self.version
        self._uv._write_version()

    def _cmd_delete(self) -> list[str]:
        assert self._uv is not None and self._npm is not None
        seen: set[str] = set()
        out: list[str] = []
        for c in self._uv._cmd_delete() + self._npm._cmd_delete():
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _cmd_build(self) -> list[str]:
        assert self._uv is not None and self._npm is not None
        return self._uv._cmd_build() + self._npm._cmd_build()

    def _cmd_install(self) -> list[str]:
        assert self._uv is not None and self._npm is not None
        return self._uv._cmd_install() + self._npm._cmd_install()

    def _cmd_publish(self) -> list[str]:
        assert self._uv is not None and self._npm is not None
        return self._uv._cmd_publish() + self._npm._cmd_publish()

    def install(self, *args, **kwargs):
        assert self._uv is not None and self._npm is not None
        logger.info(f"{self.name} install (hybrid: uv wheel + frontend deps)")
        run_shell_list(self._uv._cmd_build() + self._uv._cmd_install() + self._uv._cmd_delete())
        self._npm.install(*args, **kwargs)
