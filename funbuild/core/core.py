"""
打包的工具类
"""

import argparse
import logging
import os
import time

from funbuild.shell import run_shell, run_shell_list
from git import Repo


def package_size():
    data = [
        line
        for line in open("./setup.py", "r").read().split("\n")
        if "multi_package" in line
    ]
    if len(data) == 1:
        return int(data[0].split("=")[1])
    return 1


class PackageBuild:
    """
    打包的工具类
    """

    def __init__(self, name=None):
        self.repo_path = run_shell("git rev-parse --show-toplevel", printf=False)
        self.name = name or self.repo_path.split("/")[-1]
        self.repo = Repo(self.repo_path)
        self.git_url = [url for url in self.repo.remote().urls][0]

    def git_pull(self, args=None, **kwargs):
        """
        git pull
        """
        logging.info("{} pull".format(self.name))
        # run_shell("git pull")
        self.repo.remote().pull()

    def git_push(self, args=None, **kwargs):
        """
        git push
        """
        logging.info("{} push".format(self.name))
        run_shell_list(["git add -A", 'git commit -a -m "add"', "git push"])
        self.repo.index.add(f"{self.repo_path}/*")
        self.repo.index.commit(message="add")
        self.repo.remote().pull()

    def git_install(self, args=None, **kwargs):
        """
        git install
        """
        logging.info("{} install".format(self.name))
        run_shell_list(
            [
                "pip uninstall {} -y".format(self.name),
                # 'python3 setup.py install',
                "pip install .",
                "rm -rf *.egg-info",
                "rm -rf dist",
                "rm -rf build",
            ]
        )
        self.git_clear_build()

    def pip_install(self):
        """
        pip install
        """
        run_shell("pip install -U -q git+{}".format(self.git_url))
        logging.info("pip install {} success".format(self.name))

    def git_clear_build(self):
        logging.info("{} build clear".format(self.name))
        run_shell_list(
            [
                "rm -rf *.egg-info",
                "rm -rf dist",
                "rm -rf build",
            ]
        )

    def git_build(self, args=None, **kwargs):
        """
        git build
        """
        logging.info("{} build".format(self.name))
        self.git_pull()
        self.git_clear_build()
        size = package_size()

        for i in range(size):
            os.environ["funbuild_multi_index"] = str(i)
            run_shell_list(["python -m build --wheel -n"])  # 编译  生成 wheel 包
            run_shell_list(["rm -rf build"])
        run_shell_list(["rm -rf build", "twine upload dist/*", "pip install dist/*"])

        # if args.multi:
        #     run_shell_list(
        #         [
        #             "python setup.py bdist_wheel",
        #             "twine upload dist/*",
        #             "pip install dist/*",
        #         ]
        #     )
        # else:
        #     run_shell_list(
        #         [
        #             f"python -m build --wheel -n",  # 编译  生成 wheel 包
        #             # f"python3 {set_up_file} sdist",  # 生成 tar.gz
        #             # f'python3 {set_up_file} bdist_egg',  # 生成 egg 包
        #             # f"python3 {set_up_file} ",  #
        #             # twine register dist/*
        #             "twine upload dist/*",  # 发布包
        #             "pip install dist/*",  # 安装包
        #         ]
        #     )
        self.git_clear_build()
        self.git_push()
        self.git_tags()
        # self.pip_install()

    def git_clean_history(self, args=None, **kwargs):
        """
        git build
        """
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
        """
        git clean
        """
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


def funbuild():
    parser = argparse.ArgumentParser(prog="PROG")
    subparsers = parser.add_subparsers(help="sub-command help")

    # 添加子命令
    build_parser = subparsers.add_parser("build", help="build package")
    build_parser.add_argument(
        "--multi", default=False, action="store_true", help="build multi package"
    )
    build_parser.set_defaults(func=PackageBuild().git_build)  # 设置默认函数

    # 添加子命令
    clean_history_parser = subparsers.add_parser("clean_history", help="clean history")
    clean_history_parser.set_defaults(
        func=PackageBuild().git_clean_history
    )  # 设置默认函数

    # 添加子命令
    pull_parser = subparsers.add_parser("pull", help="git pull")
    pull_parser.add_argument("--quiet", default=True, help="quiet")
    pull_parser.set_defaults(func=PackageBuild().git_pull)  # 设置默认函数

    # 添加子命令
    push_parser = subparsers.add_parser("push", help="git push")
    push_parser.add_argument("--quiet", default=True, help="quiet")
    push_parser.set_defaults(func=PackageBuild().git_push)  # 设置默认函数

    # 添加子命令
    install_parser = subparsers.add_parser("install", help="install package")
    install_parser.set_defaults(func=PackageBuild().git_install)  # 设置默认函数

    # 添加子命令
    clear_parser = subparsers.add_parser("clear", help="clear")
    clear_parser.set_defaults(func=PackageBuild().git_clean)  # 设置默认函数
    # 添加子命令
    tag_parser = subparsers.add_parser("tag", help="git build tag")
    tag_parser.set_defaults(func=PackageBuild().git_tags)  # 设置默认函数

    args = parser.parse_args()
    args.func(args)
