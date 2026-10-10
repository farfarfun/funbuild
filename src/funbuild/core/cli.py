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
        """提交信息不合约定时提醒一句就继续, 不拦下整条发版路径。"""
        if message is not None and not is_valid_commit_message(message):
            typer.secho(f"提醒: {COMMIT_MESSAGE_HINT}; 收到 {message!r}, 按原样提交", fg=typer.colors.YELLOW, err=True)

    @cli.command()
    def upgrade(
        version: typing.Annotated[
            str | None,
            typer.Option("--version", help="指定目标版本号; 不传则沿用旧逻辑自动递增当前版本号"),
        ] = None,
    ) -> None:
        """升级版本"""
        builder().upgrade(version=version)

    @cli.command("latest-deps")
    def latest_deps() -> None:
        """把 latest-packages 里的依赖下界抬到最新版 (不构建、不发布)"""
        # build 里这一步跑在 publish 之前、改动不可见, 单独给个入口才能在真正
        # 发版之前确认配置写对了、私有 index 也解析得通。
        builder()._sync_latest_dependencies()

    @cli.command()
    def pull() -> None:
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
    ) -> None:
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
    def install() -> None:
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
    ) -> None:
        """构建发布"""
        # 提醒放在最前面, 这样不规范的信息在终端上看得见, 而不是被发布日志冲掉
        check_message(message)
        builder().build(message=message, version=version)

    # release 是 build 的别名: 复用同一函数对象而非复制签名, 避免两者日后漂移
    cli.command("release", help="构建发布 (build 的别名)")(build)

    @cli.command()
    def clean_history() -> None:
        """清理历史"""
        builder().clean_history()

    @cli.command()
    def clean() -> None:
        """清理缓存"""
        builder().clean()

    @cli.command()
    def tag() -> None:
        """创建标签"""
        builder().tags()

    cli()
