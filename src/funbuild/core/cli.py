#!/usr/bin/python3

import typing

import typer

from .registry import get_build
from .util import COMMIT_MESSAGE_HINT, NotAGitRepositoryError, is_valid_commit_message


def funbuild() -> None:
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

    def check_message(message: str | None) -> None:
        """提交信息不合规时给一句人话再退出, 而不是甩一脸 ValueError traceback。"""
        if message is not None and not is_valid_commit_message(message):
            typer.secho(f"错误: {COMMIT_MESSAGE_HINT}; 收到 {message!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

    @cli.command()
    def upgrade(
        version: typing.Annotated[
            str | None,
            typer.Option("--version", help="指定目标版本号; 不传则沿用旧逻辑自动递增当前版本号"),
        ] = None,
    ):
        """升级版本"""
        builder().upgrade(version=version)

    @cli.command()
    def pull():
        """拉取代码"""
        builder().pull()

    @cli.command()
    def push(
        target: typing.Annotated[
            str | None,
            typer.Argument(help='传 "all" 时, 先依次在每个 submodule 里执行 push, 再 push 当前仓库'),
        ] = None,
        message: typing.Annotated[
            str | None,
            typer.Option("--message", "-m", help="commit 信息; 不传则由 aicommits 依据改动自动生成"),
        ] = None,
        batch_size: typing.Annotated[
            int, typer.Option("--batch-size", min=1, help="每个提交包含的最大修改文件数")
        ] = 20,
    ):
        """推送代码"""
        if target not in (None, "all"):
            typer.secho(f'错误: push 的位置参数只接受 "all", 收到 {target!r}', fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        check_message(message)
        if target == "all":
            builder().push_all(message, batch_size=batch_size)
        else:
            builder().push(message, batch_size=batch_size)

    @cli.command()
    def install():
        """安装包"""
        builder().install()

    @cli.command()
    def build(
        message: typing.Annotated[
            str | None,
            typer.Argument(help="提交时的 commit 信息; 不传则由 aicommits 依据改动自动生成"),
        ] = None,
        version: typing.Annotated[
            str | None,
            typer.Option("--version", help="指定发布版本号; 不传则沿用旧逻辑自动递增当前版本号"),
        ] = None,
    ):
        """构建发布"""
        # 先校验: 不合规的信息要在 upgrade/构建/发布之前拦住, 否则会先把包发到
        # PyPI 再在 push 那一步抛异常, 留下「已发布但没提交」的半截状态。
        check_message(message)
        builder().build(message=message, version=version)

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
