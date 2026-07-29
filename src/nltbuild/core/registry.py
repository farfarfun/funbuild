#!/usr/bin/python3

from typing import Union

from .empty_build import EmptyBuild
from .hybrid import UvNpmHybridBuild
from .npm_frontend import NpmFrontendBuild
from .poetry_build import PoetryBuild
from .pypi_build import PypiBuild
from .util import logger
from .uv_build import UVBuild
from .version_file_build import VersionFileBuild


def get_build() -> Union[
    UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, VersionFileBuild, EmptyBuild
]:
    """获取合适的构建类"""
    # 顺序即优先级: 越靠前越具体。VersionFileBuild 作为最后的真实回退,
    # 只在所有清单类构建都不匹配时才接管; EmptyBuild 永远匹配, 必须垫底。
    builders = [UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, VersionFileBuild, EmptyBuild]
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
