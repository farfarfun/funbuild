#!/usr/bin/python3

import os
import shlex
from configparser import ConfigParser

from .base import BaseBuild
from .util import deep_create, deep_get, dump_toml, load_toml, logger
from .version_sync import root_pyproject_project_version, sync_all_manifest_versions


def _uv_bundle_out_dir(pkg_dir: str) -> str:
    """多包构建时各包 wheel 输出目录 (相对仓库根), 互不覆盖。"""
    d = os.path.normpath(pkg_dir)
    key = "root" if d in (".", "") else d.replace(os.sep, "_")
    return os.path.join("dist", "funbuild", key)


def _uv_bundle_out_dir_abs(repo_root: str, pkg_dir: str) -> str:
    """与 _uv_bundle_out_dir 相同位置, 但为绝对路径。

    uv build 在指定 --directory 为子目录时, --out-dir 按「包目录」解析相对路径,
    若传 dist/funbuild/... 会把产物写到 extbuild/foo/dist/... 而非仓库根下 dist/...,
    导致后续 uv publish 在根 dist 下找不到文件。传入绝对路径可避免该问题。
    """
    rel = _uv_bundle_out_dir(pkg_dir)
    return os.path.normpath(os.path.join(repo_root.strip(), rel))


class UVBuild(BaseBuild):
    """UV构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_paths = ["./pyproject.toml"]

        for root in ("extbuild", "exts"):
            if os.path.isdir(root):
                for file in sorted(os.listdir(root)):
                    path = os.path.join(root, file)
                    if os.path.isdir(path):
                        toml_path = os.path.join(path, "pyproject.toml")
                        if os.path.exists(toml_path):
                            self.toml_paths.append(toml_path)

    def check_type(self) -> bool:
        """检查是否为UV项目; 版本始终以根目录 [project].version 为主。"""
        if not os.path.exists(self.toml_paths[0]):
            return False
        a = load_toml(self.toml_paths[0])
        if "project" not in a:
            return False
        rv = root_pyproject_project_version()
        if rv is not None:
            self.version = rv
        else:
            pv = a["project"].get("version")
            self.version = pv.strip() if isinstance(pv, str) and pv.strip() else "0.0.1"
        return True

    def _write_version(self):
        """写入版本号到所有pyproject.toml"""
        for toml_path in self.toml_paths:
            try:
                config = load_toml(toml_path)
                self.config_format(config, os.path.dirname(toml_path) or ".")
                config["project"]["version"] = self.version
                dump_toml(config, toml_path)
            except Exception as e:
                logger.error(f"Failed to update version in {toml_path}: {e}")
                raise
        sync_all_manifest_versions(self.version)

    # LICENSE 文件的常见命名, 用于填充 PEP 639 的 project.license-files
    LICENSE_FILE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING")

    def _apply_license_metadata(self, config, pkg_dir="."):
        """按 PEP 639 (setuptools>=77) 声明许可证。

        旧写法 `[tool.setuptools] license-files = []` 会让 wheel 不带任何许可证文件;
        且 setuptools>=77 下它与 `[project].license-files` 同时存在会直接报错,
        因此这里先把它清掉再写新式字段。
        """
        tool_setuptools = deep_get(config, "tool", "setuptools")
        if isinstance(tool_setuptools, dict):
            tool_setuptools.pop("license-files", None)

        project = config.setdefault("project", {})
        license_value = project.get("license")
        if not (isinstance(license_value, str) and license_value.strip()):
            # 旧的 license = {text = "MIT"} 表写法在 PEP 639 下已废弃, 统一为 SPDX 字符串
            text = license_value.get("text") if isinstance(license_value, dict) else None
            project["license"] = text.strip() if isinstance(text, str) and text.strip() else "MIT"

        found = [name for name in self.LICENSE_FILE_NAMES if os.path.isfile(os.path.join(pkg_dir, name))]
        if found:
            project["license-files"] = found
        else:
            # 声明了却找不到文件同样会让 setuptools 构建失败
            project.pop("license-files", None)

    def config_format(self, config, pkg_dir="."):
        """格式化配置文件"""
        if not self.is_org_repo:
            return
        self._apply_license_metadata(config, pkg_dir)
        deep_create(
            config,
            "project",
            key="authors",
            value=[
                {"name": "牛哥", "email": "niuliangtao@qq.com"},
                {"name": "farfarfun", "email": "farfarfun@qq.com"},
            ],
        )
        deep_create(
            config,
            "project",
            key="maintainers",
            value=[
                {"name": "牛哥", "email": "niuliangtao@qq.com"},
                {"name": "farfarfun", "email": "farfarfun@qq.com"},
            ],
        )
        deep_create(
            config,
            "project",
            key="urls",
            value={
                "Organization": "https://github.com/farfarfun",
                "Repository": f"https://github.com/farfarfun/{self.name}",
                "Releases": f"https://github.com/farfarfun/{self.name}/releases",
            },
        )
        # description 是可选字段: uv init 之外的模板未必写它, 直接下标会抛
        # KeyError 并让整个 upgrade 失败。
        description = deep_get(config, "project", "description")
        if not isinstance(description, str) or "Add your description here" in description:
            deep_create(config, "project", key="description", value=f"{self.name}")

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return [
            *super()._cmd_delete(),
            "rm -rf src/*.egg-info",
            "rm -rf extbuild/*/src/*.egg-info",
            "rm -rf exts/*/src/*.egg-info",
        ]

    def _export_publish_credentials(self, settings) -> None:
        """把 ~/.pypirc 凭据导出为 UV_PUBLISH_* 环境变量。

        凭据不能拼进命令行: funshell 用 shell=True 执行, 完整命令行会出现在
        进程表中 (ps aux) 对本机所有用户可见; 且明文拼接遇到含引号/空格的密码
        会破坏引号甚至造成命令注入。uv 原生支持从环境变量读取。
        """
        user = settings.get("username")
        if not user:
            return
        password = settings.get("password")
        if "__token__" in user:
            if password:
                os.environ["UV_PUBLISH_TOKEN"] = password
        else:
            os.environ["UV_PUBLISH_USERNAME"] = user
            if password:
                os.environ["UV_PUBLISH_PASSWORD"] = password
        if url := settings.get("repository"):
            os.environ["UV_PUBLISH_URL"] = url

    def _cmd_publish(self) -> list[str]:
        """发布命令: 按各包构建产物目录分别 uv publish。"""
        config = ConfigParser()
        pypirc = os.path.expanduser("~/.pypirc")
        server = "pypi"
        if os.path.exists(pypirc):
            config.read(pypirc)
            if config.has_section("distutils") and "index-servers" in config["distutils"]:
                servers = config["distutils"]["index-servers"].strip().split()
                if servers:
                    server = servers[0]

        if os.path.exists(self.toml_paths[0]):
            a = load_toml(self.toml_paths[0])
            server = deep_get(a, "tool", "uv", "index", 0, "name") or server
        logger.info(f"public server: {server}")
        self._export_publish_credentials(config[server] if config.has_section(server) else {})

        dirs_seen: set[str] = set()
        cmds: list[str] = []
        root = self.repo_path.strip()
        for toml_path in self.toml_paths:
            pkg_dir = os.path.normpath(os.path.dirname(toml_path))
            if pkg_dir in dirs_seen:
                continue
            dirs_seen.add(pkg_dir)
            out_dir = _uv_bundle_out_dir_abs(root, pkg_dir)
            cmds.append(" ".join(["uv", "publish", shlex.quote(f"{out_dir}/*")]))
        return cmds

    def _cmd_build(self) -> list[str]:
        """构建命令: 依次在各包目录构建, wheel 输出到 dist/funbuild/<唯一子目录>。"""
        result = [
            "uv lock --prerelease=allow",
        ]
        if self.is_org_repo:
            result.append("uvx ruff format")
            result.append("uvx ruff clean")
        seen_pkg: set[str] = set()
        root = self.repo_path.strip()
        for toml_path in self.toml_paths:
            pkg_dir = os.path.normpath(os.path.dirname(toml_path))
            if pkg_dir in seen_pkg:
                continue
            seen_pkg.add(pkg_dir)
            out_dir = _uv_bundle_out_dir_abs(root, pkg_dir)
            result.append(
                " ".join(
                    [
                        "uv",
                        "build",
                        "-q",
                        "--wheel",
                        "--prerelease=allow",
                        "--directory",
                        shlex.quote(pkg_dir),
                        "--out-dir",
                        shlex.quote(out_dir),
                        "--clear",
                    ]
                )
            )
        return result

    def _cmd_install(self) -> list[str]:
        """安装命令: 安装各包构建产物目录下的 wheel。"""
        return ["uv pip install dist/funbuild/*/*.whl"]
