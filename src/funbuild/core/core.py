#!/usr/bin/python3

"""
打包的工具类
"""

from __future__ import annotations

import argparse
import os
from configparser import ConfigParser
from typing import List

import toml
from funutil import getLogger

from funbuild.shell import run_shell, run_shell_list

logger = getLogger("funbuild")


class BaseBuild:
    def __init__(self, name=None):
        self.repo_path = run_shell("git rev-parse --show-toplevel", printf=False)
        self.name = name or self.repo_path.split("/")[-1]
        self.version = None

    def check_type(self) -> bool:
        raise NotImplementedError

    def _write_version(self):
        raise NotImplementedError

    def __version_upgrade(self, step=128):
        version = self.version
        if version is None:
            version = "0.0.1"

        version1 = [int(i) for i in version.split(".")]
        version2 = version1[0] * step * step + version1[1] * step + version1[2] + 1

        version1[2] = version2 % step
        version1[1] = int(version2 / step) % step
        version1[0] = int(version2 / step / step)

        return "{}.{}.{}".format(*version1)

    def _cmd_build(self) -> List[str]:
        return []

    def _cmd_publish(self) -> List[str]:
        return []

    def _cmd_install(self) -> List[str]:
        return ["pip install dist/*.whl"]

    def _cmd_delete(self) -> List[str]:
        return ["rm -rf dist", "rm -rf build", "rm -rf *.egg-info"]

    def funbuild_upgrade(self, args=None, **kwargs):
        self.version = self.__version_upgrade()
        self._write_version()

    def funbuild_pull(self, args=None, **kwargs):
        logger.info(f"{self.name} pull")
        run_shell_list(["git pull"])

    def funbuild_push(self, args=None, **kwargs):
        logger.info(f"{self.name} push")
        run_shell_list(["git add -A", 'git commit -a -m "add"', "git push"])

    def funbuild_install(self, args=None, **kwargs):
        logger.info(f"{self.name} install")
        run_shell_list(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def funbuild_build(self, args=None, **kwargs):
        logger.info(f"{self.name} build")
        self.funbuild_pull()
        self.funbuild_upgrade()
        run_shell_list(
            self._cmd_delete() + self._cmd_build() + self._cmd_install() + self._cmd_publish() + self._cmd_delete()
        )
        self.funbuild_push()
        self.git_tags()

    def funbuild_clean_history(self, args=None, **kwargs):
        logger.info(f"{self.name} clean history")
        run_shell_list(
            [
                "git tag -d $(git tag -l) || true",  # 删除本地 tag
                "git fetch",  # 拉取远程tag
                "git push origin --delete $(git tag -l)",  # 删除远程tag
                "git tag -d $(git tag -l) || true",  # 删除本地tag
                "git checkout --orphan latest_branch",  # 1.Checkout
                "git add -A",  # 2.Add all the files
                'git commit -am "clear history"',  # 3.Commit the changes
                "git branch -D master",  # 4.Delete the branch
                "git branch -m master",  # 5.Rename the current branch to master
                "git push -f origin master",  # 6.Finally, force update your repository
                "git push --set-upstream origin master",
                f"echo {self.name} success",
            ]
        )

    def git_clean(self, args=None, **kwargs):
        logger.info(f"{self.name} clean")
        run_shell_list(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore'",
                "git gc --aggressive",
            ]
        )

    def git_tags(self, args=None, **kwargs):
        run_shell_list(
            [
                f"git tag v{self.version}",
                "git push --tags",
            ]
        )


class PypiBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version_path = "./script/__version__.md"

    def check_type(self):
        if os.path.exists(self.version_path):
            self.version = open(self.version_path, "r").read()  # noqa: UP015
            return True
        return False

    def _write_version(self):
        with open(self.version_path, "w") as f:
            f.write(self.version)

    def _cmd_build(self) -> List[str]:
        return []

    def _cmd_install(self) -> List[str]:
        return [
            "pip install dist/*.whl",
        ]


class PoetryBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_path = "./pyproject.toml"

    def check_type(self) -> bool:
        if os.path.exists(self.toml_path):
            a = toml.load(self.toml_path)
            if "tool" in a:
                self.version = a["tool"]["poetry"]["version"]
                return True
        return False

    def _write_version(self):
        a = toml.load(self.toml_path)
        a["tool"]["poetry"]["version"] = self.version
        with open(self.toml_path, "w") as f:
            toml.dump(a, f)

    def _cmd_publish(self) -> List[str]:
        return ["poetry publish"]

    def _cmd_build(self) -> List[str]:
        return ["poetry lock", "poetry build"]


class UVBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_paths = ["./pyproject.toml"]

        for root in ("extbuild", "exts"):
            if os.path.isdir(root):
                for file in os.listdir(root):
                    path = os.path.join(root, file)
                    if os.path.isdir(path):
                        toml_path = os.path.join(path, "pyproject.toml")
                        if os.path.exists(toml_path):
                            self.toml_paths.append(toml_path)

    def check_type(self) -> bool:
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            if "project" in a:
                self.version = a["project"]["version"]
                return True
        return False

    def _write_version(self):
        for toml_path in self.toml_paths:
            a = toml.load(toml_path)
            a["project"]["version"] = self.version
            with open(toml_path, "w") as f:
                toml.dump(a, f)

    def _cmd_delete(self) -> List[str]:
        return [*super()._cmd_delete(), "rm -rf src/*.egg-info"]

    def _cmd_publish(self) -> List[str]:
        config = ConfigParser()

        config.read(f"{os.environ['HOME']}/.pypirc")

        server = config["distutils"]["index-servers"].strip().split()[0]
        settings = config[server]
        opts = []
        if user := settings.get("username"):
            password = settings.get("password")

            if "__token__" in user:
                if password:
                    opts.append(f"--token={password}")
            else:
                opts.append(f"--username={user}")
                if password:
                    opts.append(f"--password={password}")

            url = settings.get("repository")
            if url and opts:
                opts.append(f"--publish-url={url}")
        a = ["uv", "publish", *opts]
        return [" ".join(a)]

    def _cmd_build(self) -> List[str]:
        result = ["uv lock"]
        for toml_path in self.toml_paths:
            result.append(f"uv build -q --directory {os.path.dirname(toml_path)}")
        return result

    def _cmd_install(self) -> List[str]:
        return ["uv pip install dist/*.whl"]


def get_build() -> BaseBuild:
    builders = [UVBuild, PoetryBuild, PypiBuild]
    for builder in builders:
        build = builder()
        if build.check_type():
            return build


def funbuild():
    builder = get_build()
    if builder is None:
        raise Exception("build error")

    parser = argparse.ArgumentParser(prog="PROG")
    subparsers = parser.add_subparsers(help="sub-command help")

    # 添加子命令
    build_parser = subparsers.add_parser("upgrade", help="build package")
    build_parser.set_defaults(func=builder.funbuild_upgrade)  # 设置默认函数

    # 添加子命令
    build_parser = subparsers.add_parser("build", help="build package")
    build_parser.set_defaults(func=builder.funbuild_build)  # 设置默认函数

    # 添加子命令
    clean_history_parser = subparsers.add_parser("clean_history", help="clean history")
    clean_history_parser.set_defaults(func=builder.funbuild_clean_history)  # 设置默认函数

    # 添加子命令
    pull_parser = subparsers.add_parser("pull", help="git pull")
    pull_parser.add_argument("--quiet", default=True, help="quiet")
    pull_parser.set_defaults(func=builder.funbuild_pull)  # 设置默认函数

    # 添加子命令
    push_parser = subparsers.add_parser("push", help="git push")
    push_parser.add_argument("--quiet", default=True, help="quiet")
    push_parser.set_defaults(func=builder.funbuild_push)  # 设置默认函数

    # 添加子命令
    install_parser = subparsers.add_parser("install", help="install package")
    install_parser.set_defaults(func=builder.funbuild_install)  # 设置默认函数

    # 添加子命令
    clear_parser = subparsers.add_parser("clear", help="clear")
    clear_parser.set_defaults(func=builder.git_clean)  # 设置默认函数

    # 添加子命令
    tag_parser = subparsers.add_parser("tag", help="git build tag")
    tag_parser.set_defaults(func=builder.git_tags)  # 设置默认函数

    args = parser.parse_args()
    args.func(args)
