#!/usr/bin/python3

import os
from typing import Union

from .base import git_repo_root
from .empty_build import EmptyBuild
from .flutter_build import FlutterBuild
from .hybrid import UvNpmHybridBuild
from .npm_frontend import NpmFrontendBuild
from .poetry_build import PoetryBuild
from .pypi_build import PypiBuild
from .util import logger
from .uv_build import UVBuild
from .version_file_build import VersionFileBuild


def get_build() -> Union[
    UvNpmHybridBuild,
    UVBuild,
    PoetryBuild,
    PypiBuild,
    FlutterBuild,
    NpmFrontendBuild,
    VersionFileBuild,
    EmptyBuild,
]:
    """获取合适的构建类"""
    # 先归一化到仓库根: 清单探测 (./pyproject.toml、extbuild/) 和构建命令
    # (rm -rf dist、uv build --directory .) 全部按相对路径解析。从子目录运行时
    # 这些路径统统落空, 于是静默退化成 EmptyBuild —— 退出码还是 0, 看起来像
    # 发布成功了, 实际什么都没做。
    os.chdir(git_repo_root(os.getcwd()))

    # 顺序即优先级: 越靠前越具体。FlutterBuild 排在 NpmFrontendBuild 之前 ——
    # Flutter Web 项目常常也带一个仅供前端工具链使用的 package.json, 若 npm
    # 先匹配会把它错认成纯前端项目。VersionFileBuild 作为最后的真实回退,
    # 只在所有清单类构建都不匹配时才接管; EmptyBuild 永远匹配, 必须垫底。
    builders = [
        UvNpmHybridBuild,
        UVBuild,
        PoetryBuild,
        PypiBuild,
        FlutterBuild,
        NpmFrontendBuild,
        VersionFileBuild,
        EmptyBuild,
    ]
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
