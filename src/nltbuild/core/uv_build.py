#!/usr/bin/python3

import os
import shlex
from configparser import ConfigParser

import toml

from .base import BaseBuild
from .util import deep_create, deep_get, logger
from .version_sync import root_pyproject_project_version, sync_all_manifest_versions


def _uv_bundle_out_dir(pkg_dir: str) -> str:
    """多包构建时各包 wheel 输出目录 (相对仓库根), 互不覆盖。"""
    d = os.path.normpath(pkg_dir)
    key = "root" if d in (".", "") else d.replace(os.sep, "_")
    return os.path.join("dist", "nltbuild", key)


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
        a = toml.load(self.toml_paths[0])
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
                config = toml.load(toml_path)
                self.config_format(config)
                config["project"]["version"] = self.version
                with open(toml_path, "w") as f:
                    toml.dump(config, f)
            except Exception as e:
                logger.error(f"Failed to update version in {toml_path}: {e}")
                raise
        sync_all_manifest_versions(self.version)

    def config_format(self, config):
        """格式化配置文件"""
        if not self.name.startswith("fun"):
            return
        deep_create(config, "tool", "setuptools", key="license-files", value=[])
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
        if "Add your description here" in config["project"]["description"]:
            deep_create(config, "project", key="description", value=f"{self.name}")

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return [
            *super()._cmd_delete(),
            "rm -rf src/*.egg-info",
            "rm -rf extbuild/*/src/*.egg-info",
            "rm -rf exts/*/src/*.egg-info",
        ]

    def _cmd_publish(self) -> list[str]:
        """发布命令: 按各包构建产物目录分别 uv publish。"""
        config = ConfigParser()

        config.read(f"{os.environ['HOME']}/.pypirc")

        server = config["distutils"]["index-servers"].strip().split()[0]
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            server = deep_get(a, "tool", "uv", "index", 0, "name") or server
        logger.info(f"public server: {server}")
        settings = config[server]
        opts: list[str] = []
        if user := settings.get("username"):
            password = settings.get("password")

            if "__token__" in user:
                if password:
                    opts.append(f"--token={password}")
            else:
                opts.append(f"--username={user}")
                if password:
                    opts.append(f"--password='{password}'")

            url = settings.get("repository")
            if url and opts:
                opts.append(f"--publish-url={url}")
        dirs_seen: set[str] = set()
        cmds: list[str] = []
        for toml_path in self.toml_paths:
            pkg_dir = os.path.normpath(os.path.dirname(toml_path))
            if pkg_dir in dirs_seen:
                continue
            dirs_seen.add(pkg_dir)
            out_dir = _uv_bundle_out_dir(pkg_dir)
            parts = ["uv", "publish", *opts, shlex.quote(f"{out_dir}/*")]
            cmds.append(" ".join(parts))
        return cmds

    def _cmd_build(self) -> list[str]:
        """构建命令: 依次在各包目录构建, wheel 输出到 dist/nltbuild/<唯一子目录>。"""
        result = [
            "uv lock --prerelease=allow",
        ]
        if self.name.startswith("fun"):
            result.append("uvx ruff format")
            result.append("uvx ruff clean")
        seen_pkg: set[str] = set()
        for toml_path in self.toml_paths:
            pkg_dir = os.path.normpath(os.path.dirname(toml_path))
            if pkg_dir in seen_pkg:
                continue
            seen_pkg.add(pkg_dir)
            out_dir = _uv_bundle_out_dir(pkg_dir)
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
        return ["uv pip install dist/nltbuild/*/*.whl"]
