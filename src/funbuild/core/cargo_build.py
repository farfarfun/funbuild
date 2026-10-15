#!/usr/bin/python3

import os

from .base import BaseBuild
from .cargo_version import read_cargo_version, sync_cargo_version
from .util import ShellCommandError, logger, parse_version, run_checked
from .version_sync import ManifestVersionSyncError, sync_all_manifest_versions


class CargoBuild(BaseBuild):
    """纯 Rust 仓库 (Cargo.toml 是唯一版本主源) 的构建类。

    产物是二进制而不是 Python 扩展, 所以继承 `BaseBuild` 而不是 `UVBuild` ——
    后者的 `check_type` / `toml_paths` / `_buildable_pkg_dirs()` 全依赖 pyproject。
    Rust + Python 混合的 maturin 项目归 `MaturinBuild`。

    **不发布。** 这一族仓库的发布由推 `v*` tag 触发的 GitHub Actions 完成 (多平台
    交叉构建、.deb、SHA256SUMS、GitHub Release、装机验证, 最后才是 `cargo publish`),
    funbuild 只管到「版本号 + 本地校验 + 提交 + 打 tag + 推送」为止。见 `_cmd_publish`。
    """

    MANIFEST_PATH = "./Cargo.toml"
    LOCK_PATH = "./Cargo.lock"

    # 这几样在根目录的话, 版本主源就不是 Cargo.toml: 带 pyproject 的 Rust 仓库是
    # maturin 项目 (MaturinBuild) 或者「Python 主体 + 附带一个 crate」(UVBuild),
    # 两者的版本都以 pyproject 为准, 而且它们本来就会顺着祖先链去写 Cargo.toml；
    # package.json / pubspec.yaml 同理各有归属。
    #
    # 不排除 `VERSION`: Cargo.toml 才是 cargo 和 release.yml 实际读的东西, 并排
    # 放一个 VERSION 只会是过期副本, 该由本类认领 (注册顺序在 VersionFileBuild 前)。
    FOREIGN_MANIFESTS = ("./pyproject.toml", "./package.json", "./pubspec.yaml")

    # -w/--workspace: 只重算 workspace 内的包 (就是刚改的那个版本号), 其余已经在
    # 锁文件里的依赖一个都不动。cargo 手册原话: "Other packages are updated only if
    # they don't already exist in the lockfile. This option is useful for updating
    # Cargo.lock after you've changed version numbers in Cargo.toml."
    # 不带 -w 的裸 `cargo update` 会把所有依赖升到最新兼容版 —— 那是另一件事, 不该
    # 搭着发版一起做 (发版提交里只该有版本号那一行)。
    LOCK_UPDATE = "cargo update --workspace"

    def check_type(self) -> bool:
        """根目录有 Cargo.toml、且没有别家的版本清单时认领。

        返回:
            命中返回 True (并把当前版本读进 `self.version`), 否则 False。
        """
        if not os.path.isfile(self.MANIFEST_PATH):
            return False
        for manifest in self.FOREIGN_MANIFESTS:
            if os.path.isfile(manifest):
                logger.info(f"skip CargoBuild: 根目录有 {manifest}, 版本主源不在 Cargo.toml")
                return False

        version = read_cargo_version(self.MANIFEST_PATH)
        if version is None:
            # 纯虚拟 workspace (根只有 `[workspace]`, 版本都在各成员自己手里)。认了
            # 就会打出一个不对应任何 crate 的 tag, 而 `_write_version` 根本没东西可写。
            logger.warning(
                f"skip CargoBuild: {self.MANIFEST_PATH} 里 [package] 和 [workspace.package] "
                "下都没有字面量 version; funbuild 的模型是「一个仓库一个版本号」, "
                "认领只会打出不对应任何 crate 的 tag"
            )
            return False

        try:
            parse_version(version)
        except ValueError:
            # 版本串格式不对不影响判定 —— 这是 Rust 仓库的证据已经压倒性了。真正的
            # 硬报错留给 `upgrade()` 里的 parse_version, 那时还没动过 git。
            logger.warning(f"{self.MANIFEST_PATH} 的 version 解析不了: {version!r}, 仍按 Rust 仓库处理")
        self.version = version
        return True

    def _write_version(self) -> None:
        """把版本号写进 Cargo.toml, 并让 Cargo.lock 跟上。"""
        synced = sync_cargo_version(self.MANIFEST_PATH, self.version)
        # 校验结果而不是只看返回值: 行尾注释、单引号、version 行不在 [package] /
        # [workspace.package] 下, 都会让替换悄悄落空。
        #
        # 这里必须中止, 不能像 MaturinBuild 那样只 warning —— 那边 pyproject 已经
        # 带着权威版本, Cargo 没写进去只是「crate 版本落后」; 这边 Cargo.toml 是
        # 唯一的版本主源, 放过去的后果是 tags() 照样推了 v<新版>, 然后 release.yml
        # 的版本号核对 job (grep -m1 '^version = ' Cargo.toml) 在 tag 已经公开之后
        # 才失败。
        if not synced or read_cargo_version(self.MANIFEST_PATH) != self.version:
            raise ManifestVersionSyncError(
                f"{self.MANIFEST_PATH} 里的 version 没能写成 {self.version}; "
                '要求 version 单独一行、形如 version = "x.y.z", 且在 [package] 或 '
                "[workspace.package] 下"
            )
        # 纯 Rust 仓库里是 no-op (pyproject / package.json / pubspec 已被 check_type
        # 排除掉了), 保留是为了和其它构建类型一致。
        sync_all_manifest_versions(self.version)
        self._sync_cargo_lock()

    def _sync_cargo_lock(self) -> None:
        """版本号改了之后把 Cargo.lock 一起更新。

        必须在这里做, 不能指望 `_cmd_build` 里的编译顺带刷新:

        - `funbuild upgrade` 根本不构建, 那条路径下清单和锁文件会分叉, 然后被下一次
          push 原样提交上去。
        - 失败模式是最难看的一种: release.yml 的 `cargo publish --locked` 排在整条
          发布流程的**最后**, 那时 GitHub Release、.deb、装机验证都已经过了, tag 也
          早推出去了, 什么都撤不回来。
        """
        if not os.path.isfile(self.LOCK_PATH):
            # 仓库自己不带锁文件 (库 crate 常把它 gitignore) 就别造一个: 新生成的
            # 未跟踪文件会被 push 的 `git add -A -f` 一起提交上去。
            logger.info(f"skip Cargo.lock 同步: {self.LOCK_PATH} 不在仓库里")
            return
        try:
            run_checked([f"{self.LOCK_UPDATE} --offline"], cwd=self.repo_path)
            return
        except ShellCommandError as e:
            # 冷缓存 (刚 clone) 或者刚加了新依赖时, 离线解析不出来。
            logger.warning(f"离线更新 Cargo.lock 失败, 改为联网重试: {e}")
        # 兜底仍然带 --workspace, 所以只会补齐缺失的条目, 不会改已有的钉版。
        run_checked([self.LOCK_UPDATE], cwd=self.repo_path)

    def _cmd_build(self) -> list[str]:
        """发版前的本地校验, 和仓库 ci.yml 里的检查逐字一致。

        顺序是「最快失败的排前面」: fmt 一行代码都不编译, clippy 要编 dev 全 target,
        test 还要再跑一遍。四条命令由 `run_checked` 用 ` && ` 串成一条, 任一失败都会
        在 push / tags 之前中止 —— tag 一推就触发真实发布, 不能把编译不过的版本推上去。

        不加 `--locked`: ci.yml 里也没有, 而锁文件的新鲜度已经由 `_write_version`
        里的显式同步保证了。

        也**不看 `is_org_repo`**: UVBuild / MaturinBuild 在组织仓库里会顺手跑一遍
        `uvx ruff format`, 这里刻意不做对应的 `cargo fmt --all`。本地自动改完再提交
        等于让这道关卡失去意义 —— 格式问题永远不会浮出水面; 而纯 Rust 仓库也没有
        Python 源码值得为它引入 uvx 这个依赖。
        """
        return [
            "cargo fmt --all --check",
            "cargo clippy --all-targets -- -D warnings",
            "cargo test",
        ]

    def _cmd_install(self) -> list[str]:
        """不装。

        基类默认是 `pip install dist/*.whl --force-reinstall`, 在 Rust 仓库里必然
        失败。也不用 `cargo install --path .`: 那样每次发版都要从头全量构建一遍, 还会
        覆盖掉 `~/.cargo/bin/` 里装着的那份 —— fundeploy 自己在管 apt / brew / cargo
        这几条安装渠道, 把它换成一个没有来源标记的本地构建会让它自己的 upgrade 判断
        错乱。「装得上吗」这个问题由 GitHub Actions 的装机验证对着真实产物回答。
        """
        return []

    def _cmd_publish(self) -> list[str]:
        """不发布 —— 这是本类最要紧的一条。

        crates.io 上的版本号是一次性的: 同一版本只能发一次, yank 也只是不让新依赖
        解析到它, `.crate` 永远留在上面。所以发布只能发生在一个地方, 就是 `v*` tag
        触发的 workflow, 那里 `cargo publish` 排在装机验证之后、并且会先探一次
        crates.io —— 这些条件本地命令都复现不了。
        """
        logger.info("skip publish: 纯 Rust 仓库由 tag 触发的 GitHub Actions 发布 (含 cargo publish)")
        return []

    def _cmd_delete(self) -> list[str]:
        """不清理。

        基类默认那串 `rm -rf dist / build / *.egg-info` 在 Rust 仓库里全是不存在的
        目录。`target/` 也刻意不列 (`cargo clean` 同理): `build()` 在 publish 之后
        会**再跑一次** `_cmd_delete`, 于是每次发版要付两次冷构建的代价 (release
        profile 开着 lto 和 codegen-units = 1, 一次好几分钟), 而 cargo 的增量状态是
        按 fingerprint 判定的, 不会像 `*.egg-info` 那样过期。

        `Cargo.lock` 更是绝对不能列进来: 它受版本控制, 而 publish 之后那次清理紧跟着
        就是 push, 删掉等于把「用户删了这个文件」提交上去 (`uv.lock` 踩过这个坑)。
        """
        return []

    def _published_distributions(self) -> dict[str, str]:
        """没有 Python 分发, 返回空表让 `_install_into_running_env` 变成空操作。

        基类默认是 `{self.name: self.repo_path}`, 那会拿仓库目录名去查 Python 分发;
        `fun*` 这些仓库里重名很常见, 一旦撞上就会真的去 `uv pip install <这个 Rust
        仓库>`, 然后抛一条看不懂的警告。
        """
        return {}
