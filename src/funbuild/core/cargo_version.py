#!/usr/bin/python3

"""Cargo.toml 里版本号的读与写, 由 `MaturinBuild` 和 `CargoBuild` 共用。

为什么不并进 `version_sync.py`: 那边的 `sync_all_manifest_versions` 是**所有**构建
类型都会调的, 把 Cargo.toml 加进去等于让 UVBuild / NpmFrontendBuild 也开始改仓库里
的 Cargo.toml —— 包括兄弟 crate 的。而 Rust 这边刻意只顺着祖先链走
(`MaturinBuild._cargo_manifest_paths`), 因为兄弟 crate 常有自己独立的版本线
(发到 crates.io 的 `xxx-core` 不该被包版本冲掉)。两种语义放在一个函数里只会打起来。
"""

import re

from .util import deep_get, load_toml, logger

# `version = "1.2.3"` —— 只认行首 (允许缩进) 的 version 键, 避免误伤依赖表里的
# `foo = { version = "1" }`。
CARGO_VERSION_LINE = re.compile(r'^(?P<prefix>\s*version\s*=\s*")(?P<version>[^"]*)(?P<suffix>")\s*$', re.MULTILINE)

# 只在这两个表下改 version: workspace 的 `[workspace.package]` 和单 crate 的
# `[package]`。`[dependencies]` 等表里的 version 是依赖约束, 不能跟着包版本走。
CARGO_VERSION_SECTIONS = ("[workspace.package]", "[package]")

# 读版本的优先级。`[package].version` 在前是 cargo 自己的语义: 它才是 `cargo publish`
# 上传的那个版本, 也是 fundeploy 的 release.yml 用来和 tag 比对的那个。
_CARGO_VERSION_KEYS = (("package", "version"), ("workspace", "package", "version"))


def read_cargo_version(path: str) -> str | None:
    """读出这个 Cargo.toml 声明的包版本, 没有字面量版本时返回 None。

    参数:
        path: Cargo.toml 的路径。
    返回:
        版本字符串; `[package]` 和 `[workspace.package]` 下都没有字面量 version
        时返回 None。
    """
    config = load_toml(path)  # ManifestParseError 故意不接: 清单写坏了不能当成「不是 Rust 仓库」
    for keys in _CARGO_VERSION_KEYS:
        value = deep_get(config, *keys)
        # `version.workspace = true` 取出来是个表而不是字符串, 这里挡掉, 自然落到
        # 下一个 key 去 `[workspace.package]` 找字面量。
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def sync_cargo_version(path: str, version: str) -> bool:
    """把 `[workspace.package]` / `[package]` 下的 version 改成 `version`。

    手写正则而不是 tomlkit, 两个理由:

    1. Cargo.toml 是人维护的文件, 里面的注释和排版有说明价值 (比如「为什么不开
       gzip」那段), 不值得为了改一行版本号让格式化器重排。
    2. fundeploy 的 release.yml 用 `grep -m1 '^version = ' Cargo.toml` 取版本去和
       tag 比对, 版本必须留在行首 `version = "..."` 这个形状上。

    参数:
        path: Cargo.toml 的路径。
        version: 要写入的版本号。
    返回:
        改动过返回 True; 这个文件里没有可替换的字面量 version 时返回 False。
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
        if section not in CARGO_VERSION_SECTIONS:
            out.append(line)
            continue
        replaced, count = CARGO_VERSION_LINE.subn(lambda m: f"{m.group('prefix')}{version}{m.group('suffix')}\n", line)
        changed += count
        out.append(replaced)

    if not changed:
        # 这里不警告: 单个清单没有字面量 version 是正常的 (`version.workspace = true`),
        # 该不该报由调用方判断 —— 对 maturin 是「整条回溯链一个都没改到」才报, 对
        # 纯 Rust 仓库则是唯一的版本主源没写进去, 必须中止。
        return False

    with open(path, "w", encoding="utf-8") as file:
        file.write("".join(out))
    logger.info(f"同步版本号到 {path}: {version}")
    return True
