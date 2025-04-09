#!/usr/bin/python3


import os
import traceback
import typing
from configparser import ConfigParser
from typing import List, Union

import toml
import typer
from funutil import deep_get, getLogger

from funbuild.shell import run_shell, run_shell_list

logger = getLogger("funbuild")


def aigc_commit_message(default_message: str = "add") -> str:
    try:
        from gcop.__main__ import CommitMessage, generate_commit_message, get_git_diff

        diff: str = get_git_diff("--staged")
        if not diff:
            logger.warning("No staged changes")
            return default_message
        logger.debug(f"[Code diff] \n{diff}")
        logger.debug("[On Ready] Generating commit message...")
        commit_messages: CommitMessage = generate_commit_message(diff)
        logger.debug(f"[Thought] {commit_messages.thought}")
        logger.info(f"[Generated commit message]\n{commit_messages.content}")
        return commit_messages.content
    except Exception as e:
        traceback.print_exc()
        logger.error(f"aigc_commit_message with error:{e}")
        return default_message


def deep_create(data, *args, key, value):
    """递归创建嵌套字典"""
    res = data
    for arg in args:
        if arg not in data:
            data[arg] = {}
        data = data[arg]
    data[key] = value
    return res


class BaseBuild:
    """构建工具的基类"""

    def __init__(self, name=None):
        # 获取git仓库根目录路径
        self.repo_path = run_shell("git rev-parse --show-toplevel", printf=False)
        # 设置项目名称,默认为仓库名
        self.name = name or self.repo_path.split("/")[-1]
        self.version = None

    def check_type(self) -> bool:
        """检查是否为当前构建类型"""
        raise NotImplementedError

    def _write_version(self):
        """写入版本号"""
        raise NotImplementedError

    def __version_upgrade(self, step=128):
        """版本号自增"""
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
        """构建命令"""
        return []

    def _cmd_publish(self) -> List[str]:
        """发布命令"""
        return []

    def _cmd_install(self) -> List[str]:
        """安装命令"""
        return ["pip install dist/*.whl --force-reinstall"]

    def _cmd_delete(self) -> List[str]:
        """清理命令"""
        return ["rm -rf dist", "rm -rf build", "rm -rf *.egg-info", "rm -rf uv.lock"]

    def upgrade(self, *args, **kwargs):
        """升级版本"""
        self.version = self.__version_upgrade()
        self._write_version()

    def pull(self, *args, **kwargs):
        """拉取代码"""
        logger.info(f"{self.name} pull")
        run_shell_list(["git pull"])

    def push(self, message="add", *args, **kwargs):
        """推送代码"""
        logger.info(f"{self.name} push")
        run_shell_list(["git add -A"])
        run_shell_list(
            [
                f'git commit -a -m "{aigc_commit_message(default_message=message)}"',
                "git push",
            ]
        )

    def install(self, *args, **kwargs):
        """安装包"""
        logger.info(f"{self.name} install")
        run_shell_list(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def build(self, message="add", *args, **kwargs):
        """构建发布流程"""
        logger.info(f"{self.name} build")
        self.pull()
        self.upgrade()
        run_shell_list(
            self._cmd_delete() + self._cmd_build() + self._cmd_install() + self._cmd_publish() + self._cmd_delete()
        )
        self.push(message=message)
        self.tags()

    def clean_history(self, *args, **kwargs):
        """清理git历史记录"""
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

    def clean(self, *args, **kwargs):
        """清理git缓存"""
        logger.info(f"{self.name} clean")
        run_shell_list(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore'",
                "git gc --aggressive",
            ]
        )

    def tags(self, *args, **kwargs):
        """创建版本标签"""
        run_shell_list(
            [
                f"git tag v{self.version}",
                "git push --tags",
            ]
        )


class PypiBuild(BaseBuild):
    """PyPI构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version_path = "./script/__version__.md"

    def check_type(self):
        """检查是否为PyPI项目"""
        if os.path.exists(self.version_path):
            self.version = open(self.version_path, "r").read()  # noqa: UP015
            return True
        return False

    def _write_version(self):
        """写入版本号到文件"""
        with open(self.version_path, "w") as f:
            f.write(self.version)

    def _cmd_build(self) -> List[str]:
        """构建命令"""
        return []

    def _cmd_install(self) -> List[str]:
        """安装命令"""
        return [
            "pip install dist/*.whl",
        ]


class PoetryBuild(BaseBuild):
    """Poetry构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_path = "./pyproject.toml"

    def check_type(self) -> bool:
        """检查是否为Poetry项目"""
        if os.path.exists(self.toml_path):
            a = toml.load(self.toml_path)
            if "tool" in a:
                self.version = a["tool"]["poetry"]["version"]
                return True
        return False

    def _write_version(self):
        """写入版本号到pyproject.toml"""
        a = toml.load(self.toml_path)
        a["tool"]["poetry"]["version"] = self.version
        with open(self.toml_path, "w") as f:
            toml.dump(a, f)

    def _cmd_publish(self) -> List[str]:
        """发布命令"""
        return ["poetry publish"]

    def _cmd_build(self) -> List[str]:
        """构建命令"""
        return ["poetry lock", "poetry build"]


class UVBuild(BaseBuild):
    """UV构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_paths = ["./pyproject.toml"]

        # 扫描extbuild和exts目录下的pyproject.toml
        for root in ("extbuild", "exts"):
            if os.path.isdir(root):
                for file in os.listdir(root):
                    path = os.path.join(root, file)
                    if os.path.isdir(path):
                        toml_path = os.path.join(path, "pyproject.toml")
                        if os.path.exists(toml_path):
                            self.toml_paths.append(toml_path)

    def check_type(self) -> bool:
        """检查是否为UV项目"""
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            if "project" in a:
                self.version = a["project"]["version"]
                return True
        return False

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
                raise  # 重要错误应该向上传播

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
                {"name": "niuliangtao", "email": "niuliangtao@qq.com"},
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

    def _cmd_delete(self) -> List[str]:
        """清理命令"""
        return [*super()._cmd_delete(), "rm -rf src/*.egg-info"]

    def _cmd_publish(self) -> List[str]:
        """发布命令"""
        config = ConfigParser()

        config.read(f"{os.environ['HOME']}/.pypirc")

        server = config["distutils"]["index-servers"].strip().split()[0]
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            server = deep_get(a, "tool", "uv", "index", 0, "name") or server

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
                    opts.append(f"--password='{password}'")

            url = settings.get("repository")
            if url and opts:
                opts.append(f"--publish-url={url}")
        a = ["uv", "publish", *opts]
        return [" ".join(a)]

    def _cmd_build(self) -> List[str]:
        """构建命令"""
        result = ["uv sync --prerelease=allow -U", "uv lock --prerelease=allow"]
        if self.name.startswith("fun"):
            # result.append("uv run ruff clean")
            result.append("uv run ruff format")
            result.append("uv run ruff clean")
        for toml_path in self.toml_paths:
            result.append(f"uv build -q --wheel --prerelease=allow --directory {os.path.dirname(toml_path)}")
        return result

    def _cmd_install(self) -> List[str]:
        """安装命令"""
        return ["uv pip install dist/*.whl"]


def get_build() -> Union[UVBuild, PoetryBuild, PypiBuild]:
    """获取合适的构建类"""
    builders = [UVBuild, PoetryBuild, PypiBuild]
    for builder in builders:
        build = builder()
        if build.check_type():
            return build
    raise Exception("未找到合适的构建类")


def funbuild():
    """主入口函数"""
    builder = get_build()
    cli = typer.Typer(help='build tool for "fun"')

    @cli.command()
    def upgrade():
        """升级版本"""
        builder.upgrade()

    @cli.command()
    def pull():
        """拉取代码"""
        builder.pull()

    @cli.command()
    # @click.option("--message", type=str, default="add", help="commit message")
    # @click.option("--name", type=str, default="fun", help="build name")
    def push(
        message: str = "add",
    ):
        """推送代码"""
        builder.push(
            message,
        )

    @cli.command()
    def install():
        """安装包"""
        builder.install()

    @cli.command()
    def build(
        message: typing.Annotated[str, typer.Argument(help="提交时的commit信息")] = "add",
    ):
        """构建发布"""
        builder.build(message=message)

    @cli.command()
    def clean_history():
        """清理历史"""
        builder.clean_history()

    @cli.command()
    def clean():
        """清理缓存"""
        builder.clean()

    @cli.command()
    def tags():
        """创建标签"""
        builder.tags()

    cli()
