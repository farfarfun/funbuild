#!/usr/bin/python3

from typing import Union

from .empty_build import EmptyBuild
from .hybrid import UvNpmHybridBuild
from .npm_frontend import NpmFrontendBuild
from .poetry_build import PoetryBuild
from .pypi_build import PypiBuild
from .util import logger
from .uv_build import UVBuild


def get_build() -> Union[UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]:
    """获取合适的构建类"""
    builders = [UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]
    for builder in builders:
        # 单个 builder 探测出错不应中断整条链, 否则会因某个不相关的清单文件异常
        # 而让本可正确匹配的后续 builder 没有机会被尝试。
        try:
            build = builder()
            if build.check_type():
                return build
        except Exception as e:
            logger.warning(f"{builder.__name__} check_type failed, skipped: {e}")

    raise RuntimeError("未找到合适的构建类")
