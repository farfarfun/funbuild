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
