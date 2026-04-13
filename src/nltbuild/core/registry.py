#!/usr/bin/python3

from typing import Union

from .empty_build import EmptyBuild
from .hybrid import UvNpmHybridBuild
from .npm_frontend import NpmFrontendBuild
from .poetry_build import PoetryBuild
from .pypi_build import PypiBuild
from .uv_build import UVBuild


def get_build() -> Union[UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]:
    """获取合适的构建类"""
    builders = [UvNpmHybridBuild, UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]
    for builder in builders:
        build = builder()
        if build.check_type():
            return build

    raise Exception("未找到合适的构建类")
