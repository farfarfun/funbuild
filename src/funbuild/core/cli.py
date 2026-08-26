#!/usr/bin/python3

import typing

import typer

from .registry import get_build
from .util import NotAGitRepositoryError


def funbuild():
    """主入口函数"""
    cli = typer.Typer(help='build tool for "fun"')

    # 延迟探测: get_build 要跑 git 并解析仓库里所有清单文件, 而 `--help`、
    # 参数错误、`--install-completion` 等根本用不到 builder。在这些路径上提前
    # 探测既拖慢启动, 又会让不在 git 仓库里时连 `--help` 都失败。
    cached: list = []

    def builder():
        if not cached:
            try:
                cached.append(get_build())
            except NotAGitRepositoryError as e:
                # 裸抛会甩用户一脸 traceback, 而这只是「跑错目录了」
                typer.secho(f"错误: {e}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from None
        return cached[0]

    @cli.command()
    def upgrade():
        """升级版本"""
        builder().upgrade()

    @cli.command()
    def pull():
        """拉取代码"""
        builder().pull()

    @cli.command()
    def push(
        message: typing.Annotated[
            typing.Optional[str],
            typer.Option("--message", "-m", help="commit 信息; 不传则由 aicommits 依据改动自动生成"),
        ] = None,
        batch_size: typing.Annotated[
            int, typer.Option("--batch-size", min=1, help="每个提交包含的最大修改文件数")
        ] = 20,
    ):
        """推送代码"""
        builder().push(
            message,
            batch_size=batch_size,
        )

    @cli.command()
    def install():
        """安装包"""
        builder().install()

    @cli.command()
    def build(
        message: typing.Annotated[
            typing.Optional[str],
            typer.Argument(help="提交时的 commit 信息; 不传则由 aicommits 依据改动自动生成"),
        ] = None,
    ):
        """构建发布"""
        builder().build(message=message)

    # release 是 build 的别名: 复用同一函数对象而非复制签名, 避免两者日后漂移
    cli.command("release", help="构建发布 (build 的别名)")(build)

    @cli.command()
    def clean_history():
        """清理历史"""
        builder().clean_history()

    @cli.command()
    def clean():
        """清理缓存"""
        builder().clean()

    @cli.command()
    def tag():
        """创建标签"""
        builder().tags()

    cli()
