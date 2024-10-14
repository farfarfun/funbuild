"""
打包的工具类
"""

import argparse
import logging
import os
import time
from typing import List

import toml
from git import Repo

from funbuild.core.upgrade import version_upgrade
from funbuild.shell import run_shell, run_shell_list


class BaseBuild:
    def __init__(self, name=None):
        self.repo_path = run_shell("git rev-parse --show-toplevel", printf=False)
        self.name = name or self.repo_path.split("/")[-1]
        self.repo = Repo(self.repo_path)
        self.git_url = [url for url in self.repo.remote().urls][0]
        self.version = None

    def check_type(self) -> bool:
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
        self.__version_upgrade()

    def funbuild_pull(self, args=None, **kwargs):
        logging.info("{} pull".format(self.name))
        self.repo.remote().pull()

    def funbuild_push(self, args=None, **kwargs):
        logging.info(f"{self.name} push")
        run_shell_list(["git add -A", 'git commit -a -m "add"', "git push"])

    def funbuild_install(self, args=None, **kwargs):
        logging.info(f"{self.name} install")
        run_shell_list(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def funbuild_build(self, args=None, **kwargs):
        logging.info(f"{self.name} build")
        self.funbuild_pull()
        version_upgrade()
        run_shell_list(self._cmd_build() + self._cmd_publish() + self._cmd_install() + self._cmd_delete())
        self.funbuild_push()
        self.git_tags()

    def funbuild_clean_history(self, args=None, **kwargs):
        logging.info(f"{self.name} clean history")
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
        logging.info("{} clean".format(self.name))
        run_shell_list(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore'",
                "git gc --aggressive",
            ]
        )

    def git_tags(self, args=None, **kwargs):
        self.repo.create_tag(time.strftime("%Y%m%d%H%M%S", time.localtime()))
        self.repo.remote().push()
        self.repo.remote().push(self.repo.tags)

    def git_tags_clear(self, args=None, **kwargs):
        for tag in self.repo.tags:
            self.repo.delete_tag(tag)
        self.repo.remote().push()
        self.repo.remote().push(self.repo.tags)


class PypiBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self):
        version_path = "./script/__version__.md"
        if os.path.exists(version_path):
            self.version = open(version_path, "r").read()
            return True
        return False

    def _cmd_build(self) -> List[str]:
        return []

    def _cmd_install(self) -> List[str]:
        return [
            "pip install dist/*.whl",
        ]


class PoetryBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self) -> bool:
        toml_path = "./pyproject.toml"
        if os.path.exists(toml_path):
            a = toml.load(toml_path)
            if "tool" in a:
                self.version = a["tool"]["poetry"]["version"]
                return True
        return False

    def _cmd_publish(self) -> List[str]:
        return ["poetry publish"]

    def _cmd_build(self) -> List[str]:
        return ["poetry lock", "poetry build"]


class UVBuild(BaseBuild):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self) -> bool:
        toml_path = "./pyproject.toml"
        if os.path.exists(toml_path):
            a = toml.load(toml_path)
            if "project" in a:
                self.version = a["project"]["version"]
                return True
        return False

    def _cmd_delete(self) -> List[str]:
        return super()._cmd_delete() + ["rm -rf src/*.egg-info"]

    def _cmd_publish(self) -> List[str]:
        return ["uv publish"]

    def _cmd_build(self) -> List[str]:
        return ["uv lock", "uv build"]

    def _cmd_install(self) -> List[str]:
        return ["uv pip install dist/*.whl"]


def get_build() -> BaseBuild:
    builders = [UVBuild, PoetryBuild, PypiBuild]
    for builder in builders:
        build = builder()
        if build.check_type():
            print(f"bulider:{str(build)}")
            return build


def funbuild():
    builder = get_build()
    if builder is None:
        raise Exception(f"build error")

    parser = argparse.ArgumentParser(prog="PROG")
    subparsers = parser.add_subparsers(help="sub-command help")

    # 添加子命令
    build_parser = subparsers.add_parser("upgrade", help="build package")
    build_parser.set_defaults(func=version_upgrade)  # 设置默认函数

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
