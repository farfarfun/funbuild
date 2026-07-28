#!/usr/bin/python3

import typing

import typer

from .registry import get_build


def nltbuild():
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
    def push(
        message: str = "add",
        batch_size: typing.Annotated[
            int, typer.Option("--batch-size", min=1, help="每个提交包含的最大修改文件数")
        ] = 20,
    ):
        """推送代码"""
        builder.push(
            message,
            batch_size=batch_size,
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

    # release 是 build 的别名: 复用同一函数对象而非复制签名, 避免两者日后漂移
    cli.command("release", help="构建发布 (build 的别名)")(build)

    @cli.command()
    def clean_history():
        """清理历史"""
        builder.clean_history()

    @cli.command()
    def clean():
        """清理缓存"""
        builder.clean()

    @cli.command()
    def tag():
        """创建标签"""
        builder.tags()

    cli()
