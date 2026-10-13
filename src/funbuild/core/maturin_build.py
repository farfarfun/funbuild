#!/usr/bin/python3

import os
import re
import shlex

from .util import deep_get, load_toml, logger
from .uv_build import UVBuild, _uv_bundle_out_dir_abs

# `version = "1.2.3"` —— 只认行首 (允许缩进) 的 version 键, 避免误伤依赖表里的
# `foo = { version = "1" }`。
_CARGO_VERSION_LINE = re.compile(r'^(?P<prefix>\s*version\s*=\s*")(?P<version>[^"]*)(?P<suffix>")\s*$', re.MULTILINE)

# 只在这两个表下改 version: workspace 的 `[workspace.package]` 和单 crate 的
# `[package]`。`[dependencies]` 等表里的 version 是依赖约束, 不能跟着包版本走。
_CARGO_VERSION_SECTIONS = ("[workspace.package]", "[package]")


class MaturinBuild(UVBuild):
    """maturin (PyO3) 项目的构建类。

    与 `UVBuild` 的关键差异只有三处:

    1. **构建用 maturin 而不是 uv build。** `uv build` 会走 PEP 517 调 maturin,
       能出 wheel, 但没法指定 target / manylinux, 对后面的交叉编译没有余地。
    2. **不发布。** 这是最要紧的一条 —— 见 `_cmd_publish`。
    3. **版本号还要写进 Cargo.toml**, 否则 wheel 的版本和 crate 的版本会分叉。
    """

    def check_type(self) -> bool:
        """pyproject 声明了 maturin 即认领。

        必须排在 `UVBuild` 之前注册: `UVBuild.check_type()` 只要求存在
        `[project]`, 会把 maturin 项目一并认领走, 然后跑本地 `uv publish`。
        """
        if not super().check_type():
            return False

        config = load_toml(self.toml_paths[0])
        backend = deep_get(config, "build-system", "build-backend")
        declared = isinstance(backend, str) and backend.strip().startswith("maturin")
        return declared or isinstance(deep_get(config, "tool", "maturin"), dict)

    def _cargo_manifest_path(self, pkg_dir: str) -> str:
        """`pkg_dir` 下 maturin 要用的 Cargo.toml。

        读 `[tool.maturin].manifest-path` (相对 pkg_dir 解析), 默认 pkg_dir/Cargo.toml。
        虚拟工作区 (根目录只有 `[workspace]`、没有 `[package]`) 必须显式配这一项,
        否则 maturin 会对着根清单报 `missing field package`。
        """
        toml_path = os.path.join(pkg_dir, "pyproject.toml")
        manifest = None
        if os.path.exists(toml_path):
            manifest = deep_get(load_toml(toml_path), "tool", "maturin", "manifest-path")
        rel = manifest.strip() if isinstance(manifest, str) and manifest.strip() else "Cargo.toml"
        return os.path.normpath(os.path.join(pkg_dir, rel))

    def _cargo_manifest_paths(self) -> list[str]:
        """需要同步版本号的 Cargo.toml, 从扩展 crate 的清单一路回溯到包目录。

        回溯是必须的: 扩展 crate 一般写 `version.workspace = true`, 字面量版本号
        在 workspace 根的 `[workspace.package]` 里。只改 crate 清单会一个字都改不动,
        而 pyproject 那边的版本号已经写进去了 —— wheel 和 crate 就此分叉。

        只走**祖先**目录, 不扫整个 workspace: 兄弟 crate 可能有意维护独立的版本线
        (funget-core 就是发到 crates.io 的独立 0.x), 不能被包版本号冲掉。
        """
        paths: list[str] = []
        for pkg_dir in self._buildable_pkg_dirs():
            stop = os.path.normpath(pkg_dir)
            directory = os.path.dirname(self._cargo_manifest_path(pkg_dir)) or "."
            while True:
                candidate = os.path.normpath(os.path.join(directory, "Cargo.toml"))
                if os.path.exists(candidate) and candidate not in paths:
                    paths.append(candidate)
                parent = os.path.dirname(directory) or "."
                if os.path.normpath(directory) == stop or parent == directory:
                    break
                directory = parent
        return paths

    def _sync_cargo_version(self, path: str) -> bool:
        """把 `[workspace.package]` / `[package]` 下的 version 改成当前版本。

        手写正则而不是 tomlkit: Cargo.toml 是人维护的文件, 里面的注释和排版有说明
        价值 (比如「为什么不开 gzip」那段), 不值得为了改一行版本号让格式化器重排。
        """
        with open(path, encoding="utf-8") as file:
            raw = file.read()

        section: str | None = None
        changed = 0
        out: list[str] = []
        for line in raw.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
                out.append(line)
                continue
            if section not in _CARGO_VERSION_SECTIONS:
                out.append(line)
                continue
            replaced, count = _CARGO_VERSION_LINE.subn(
                lambda m: f"{m.group('prefix')}{self.version}{m.group('suffix')}\n", line
            )
            changed += count
            out.append(replaced)

        if not changed:
            # 这里不警告: 单个清单没有字面量 version 是正常的 (`version.workspace = true`),
            # 真正该报的是整条回溯链上一个都没改到 —— 由 `_write_version` 统一判断。
            return False

        with open(path, "w", encoding="utf-8") as file:
            file.write("".join(out))
        logger.info(f"同步版本号到 {path}: {self.version}")
        return True

    def _write_version(self) -> None:
        """写 pyproject 之外, 还要写 Cargo.toml。"""
        super()._write_version()
        paths = self._cargo_manifest_paths()
        # 不用 any(generator): 那样会在第一个成功的清单上短路, 后面的就不写了。
        synced = [self._sync_cargo_version(path) for path in paths]
        if paths and not any(synced):
            # 不静默放过: 版本不同步的后果是 wheel 里的 metadata 和 crate 版本对不上,
            # 而这个错误只有在发布之后才会被发现。
            logger.warning(f"{', '.join(paths)} 里都没有找到可同步的 version 字段, 版本号未写入")

    def _cmd_build(self) -> list[str]:
        """构建命令: maturin build --release。

        产物目录沿用 `UVBuild` 的 `dist/funbuild/<key>/`, 这样 `_cmd_install`
        的 `dist/funbuild/*/*.whl` 不用改就能装上。

        不跑 `uv lock`: maturin 项目的依赖解析在 Cargo.lock 里, 而那个文件要进
        仓库、由 CI 的 `--locked` 校验, 不能在每次发版时删掉重建。
        """
        result: list[str] = []
        if self.is_org_repo:
            result.append("uvx ruff format")
            result.append("uvx ruff clean")

        root = self.repo_path.strip()
        for pkg_dir in self._buildable_pkg_dirs():
            out_dir = _uv_bundle_out_dir_abs(root, pkg_dir)
            result.append(f"rm -rf {shlex.quote(out_dir)}")
            result.append(
                " ".join(
                    [
                        "maturin",
                        "build",
                        "--release",
                        "--manifest-path",
                        shlex.quote(self._cargo_manifest_path(pkg_dir)),
                        "--out",
                        shlex.quote(out_dir),
                    ]
                )
            )
        return result

    def _cmd_publish(self) -> list[str]:
        """不在本地发布 —— 交给 tag 触发的 GitHub Actions。

        这是 maturin 项目和普通 Python 包最本质的区别: 本地 `uv publish` 只能上传
        一个本机平台的 wheel, 其他平台 `pip install` 会退化到 sdist 现场编译, 要求
        用户装 Rust 工具链 —— 对绝大多数下游来说等同于装不上。

        `build()` 后面的 `push` + `tags` 照常执行, `v<version>` 标签会触发
        release workflow 跑完整平台矩阵再上传 PyPI。
        """
        logger.info("skip publish: maturin 项目由 tag 触发的 GitHub Actions 发布多平台 wheel")
        return []
