#!/usr/bin/python3

import json
import os
import re
import subprocess

import yaml

from .base import BaseBuild
from .util import deep_get, dump_toml, load_toml, logger, parse_version

# PEP 508 依赖声明串的包名部分, 如 "funlesson-core>=1.0.0" -> "funlesson-core",
# "funlesson_core[extra]" -> "funlesson_core"。
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+")


def _normalize_package_name(name: str) -> str:
    """PEP 503 归一化: 大小写、`_`/`.`/`-` 的写法差异不应影响是否命中配置。

    npm/pub 的包名惯例本就是全小写, 借同一套归一化规则统一比对, 不需要
    再为每个生态单独维护一套大小写/分隔符规则。
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


class SubmoduleWorkspaceBuild(BaseBuild):
    """`<product>-dev` 编排仓库: `apps/` 下每个 app 是独立仓库的 git submodule。

    仓库自身没有可构建产物, `scripts/funbuild.toml` 里的 `version` 是这个工作区
    统一分发给所有 app 的共享版本, 而不是每个 app 各自维护版本。`build()` 因此
    完全不走 `_cmd_build`/`_cmd_publish` 那套单仓库 shell 命令管线, 而是: 递增
    共享版本号 -> 对 `apps/` 下每个已初始化、非嵌套 workspace 的 submodule, 把
    `packages` 里配置的依赖包升级到最新版 (uv/npm/flutter 均可) -> 转发
    `funbuild build --version <共享版本号>` -> 最后在当前仓库统一 push、打 tag
    一次, 使子模块指针更新落成一个提交。

    嵌套的 `<something>-dev` workspace (自己也有 apps/ + scripts/setup.sh) 会被
    跳过、不递归进入 —— 它有自己的一套发布节奏, 由它自己的 scripts/setup.sh 管理。
    """

    CONFIG_PATH = "scripts/funbuild.toml"

    def check_type(self) -> bool:
        if not os.path.isdir(os.path.join(self.repo_path, "apps")):
            return False
        config_path = os.path.join(self.repo_path, self.CONFIG_PATH)
        if not os.path.isfile(config_path):
            return False
        try:
            data = load_toml(config_path)
        except Exception as e:
            logger.warning(f"skip {config_path}: {e}")
            return False
        raw = data.get("version")
        if not raw:
            self.version = None
            return True
        if not isinstance(raw, str):
            logger.warning(f"skip {config_path}, version must be a string, got {raw!r}")
            return False
        try:
            parse_version(raw)
        except ValueError:
            logger.warning(f"skip {config_path}, cannot parse version {raw!r}")
            return False
        # 去掉可能的 v 前缀: tags() 会自行拼 v, 否则会得到 vv0.1.7
        self.version = raw[1:] if raw.startswith("v") else raw
        return True

    def _write_version(self):
        config_path = os.path.join(self.repo_path, self.CONFIG_PATH)
        config = load_toml(config_path)
        config["version"] = self.version
        dump_toml(config, config_path)

    def _cmd_build(self) -> list[str]:
        return []

    def _cmd_install(self) -> list[str]:
        return []

    def _cmd_publish(self) -> list[str]:
        return []

    def _cmd_delete(self) -> list[str]:
        return []

    def _pinned_packages(self) -> list[str]:
        """`scripts/funbuild.toml` 里 `packages` 字段配置的包名列表: apps/ 下任何
        submodule (不论 uv/npm/flutter) 若依赖这些包, build 前会把它们升级到最新版,
        而不是继续用各自 manifest 里可能早已过期的版本约束。
        """
        config_path = os.path.join(self.repo_path, self.CONFIG_PATH)
        try:
            data = load_toml(config_path)
        except Exception as e:
            logger.warning(f"skip {config_path}: {e}")
            return []
        packages = data.get("packages")
        if not isinstance(packages, list):
            return []
        return [pkg for pkg in packages if isinstance(pkg, str) and pkg.strip()]

    def _dependency_names_uv(self, pyproject_path: str) -> set[str]:
        """某 app `pyproject.toml` 里 `[project.dependencies]` 引用的包名集合
        (已做 PEP 503 归一化), 用于和 `_pinned_packages()` 比对。
        """
        try:
            config = load_toml(pyproject_path)
        except Exception as e:
            logger.warning(f"skip reading dependencies from {pyproject_path}: {e}")
            return set()
        deps = deep_get(config, "project", "dependencies") or []
        names = set()
        for dep in deps:
            if not isinstance(dep, str):
                continue
            match = _DEP_NAME_RE.match(dep.strip())
            if match:
                names.add(_normalize_package_name(match.group(0)))
        return names

    def _dependency_names_npm(self, package_json_path: str) -> set[str]:
        """某 app `package.json` 里 `dependencies`/`devDependencies` 的包名集合。"""
        try:
            with open(package_json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"skip reading dependencies from {package_json_path}: {e}")
            return set()
        names = set()
        for field in ("dependencies", "devDependencies"):
            deps = data.get(field)
            if isinstance(deps, dict):
                names.update(_normalize_package_name(name) for name in deps if isinstance(name, str))
        return names

    def _dependency_names_flutter(self, pubspec_path: str) -> set[str]:
        """某 app `pubspec.yaml` 里 `dependencies`/`dev_dependencies` 的包名集合,
        排除 `flutter`/`flutter_test` 这类 SDK 自带依赖 (它们不是 pub.dev 发布包,
        永远不会命中 `packages` 配置, 排除只是避免无意义的比对)。
        """
        try:
            with open(pubspec_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"skip reading dependencies from {pubspec_path}: {e}")
            return set()
        if not isinstance(data, dict):
            return set()
        names = set()
        for field in ("dependencies", "dev_dependencies"):
            deps = data.get(field)
            if isinstance(deps, dict):
                names.update(
                    _normalize_package_name(name)
                    for name in deps
                    if isinstance(name, str) and name not in ("flutter", "flutter_test")
                )
        return names

    @staticmethod
    def _detect_npm_pm(path: str) -> str:
        if os.path.exists(os.path.join(path, "pnpm-lock.yaml")):
            return "pnpm"
        if os.path.exists(os.path.join(path, "yarn.lock")):
            return "yarn"
        return "npm"

    def _upgrade_pinned_uv(self, path: str, pyproject_path: str, pinned_normalized: dict[str, str]) -> None:
        """`uv add <pkg>@latest` 不是 uv 的合法语法: `@` 后面的 token 会被当成
        PEP 508 直接引用 (本地路径/URL), 不是 npm 风格的 "latest" 关键字, 实测
        会报 `Distribution not found at: file://<cwd>/latest`。正确做法是
        `uv remove` 再 `uv add`: 不带版本号的 `uv add` 会按该 app 自己
        pyproject.toml 里配置的 index 解析出当前最新版本并重写依赖声明,
        `remove` 是让它在已存在同名依赖时也一定按新解析结果重写 (纯 `uv add`
        对已存在的依赖是空操作, 不会更新版本)。
        """
        matched = pinned_normalized.keys() & self._dependency_names_uv(pyproject_path)
        for normalized in sorted(matched):
            pkg = pinned_normalized[normalized]
            logger.info(f"upgrade pinned dependency to latest (uv): {pkg} ({path})")
            subprocess.run(["uv", "remove", pkg], cwd=path, check=True)
            subprocess.run(["uv", "add", pkg], cwd=path, check=True)

    def _upgrade_pinned_npm(self, path: str, package_json_path: str, pinned_normalized: dict[str, str]) -> None:
        matched = pinned_normalized.keys() & self._dependency_names_npm(package_json_path)
        if not matched:
            return
        pm = self._detect_npm_pm(path)
        for normalized in sorted(matched):
            pkg = pinned_normalized[normalized]
            logger.info(f"upgrade pinned dependency to latest ({pm}): {pkg} ({path})")
            if pm == "pnpm":
                subprocess.run(["pnpm", "add", f"{pkg}@latest"], cwd=path, check=True)
            elif pm == "yarn":
                subprocess.run(["yarn", "add", f"{pkg}@latest"], cwd=path, check=True)
            else:
                subprocess.run(["npm", "install", f"{pkg}@latest", "--save"], cwd=path, check=True)

    def _upgrade_pinned_flutter(self, path: str, pubspec_path: str, pinned_normalized: dict[str, str]) -> None:
        """委托给 `dart pub add <pkg>`: 包已在依赖里时, pub 会把约束重写为解析到
        的最新可用版本, 效果上等同于 uv 那边的 `remove` + `add`。
        """
        matched = pinned_normalized.keys() & self._dependency_names_flutter(pubspec_path)
        for normalized in sorted(matched):
            pkg = pinned_normalized[normalized]
            logger.info(f"upgrade pinned dependency to latest (pub): {pkg} ({path})")
            subprocess.run(["dart", "pub", "add", pkg], cwd=path, check=True)

    def _upgrade_pinned_packages(self, path: str) -> None:
        """把 `path` 这个 app 依赖里命中 `_pinned_packages()` 的包升级到最新版,
        依据 app 目录下存在的 manifest 文件分派到对应生态 (uv/npm/flutter),
        一个 app 里存在多种 manifest (如 flutter app 内嵌一个前端子项目) 时都会
        分别处理, 互不影响。命中依赖名单为空时直接跳过。
        """
        pinned = self._pinned_packages()
        if not pinned:
            return
        pinned_normalized = {_normalize_package_name(pkg): pkg for pkg in pinned}

        pyproject_path = os.path.join(path, "pyproject.toml")
        if os.path.isfile(pyproject_path):
            self._upgrade_pinned_uv(path, pyproject_path, pinned_normalized)

        package_json_path = os.path.join(path, "package.json")
        if os.path.isfile(package_json_path):
            self._upgrade_pinned_npm(path, package_json_path, pinned_normalized)

        pubspec_path = os.path.join(path, "pubspec.yaml")
        if os.path.isfile(pubspec_path):
            self._upgrade_pinned_flutter(path, pubspec_path, pinned_normalized)

    def _is_nested_workspace(self, path: str) -> bool:
        """结构性判断, 不看名字: 自己也有 apps/ + scripts/setup.sh 的 submodule
        是嵌套工作区, 递归发布是它自己的事, 这里只跳过、绝不代它分发。
        """
        return os.path.isdir(os.path.join(path, "apps")) and os.path.isfile(os.path.join(path, "scripts/setup.sh"))

    def _switch_to_tracked_branch(self, path: str) -> None:
        """submodule 常年处于 detached HEAD (如 `git submodule update --remote`
        之后), 此时子进程 `funbuild build` 内部的 `git pull`/`git push` 都没有
        可用的上游分支。只在确实 detached 时才切换到远端默认分支, 已经在分支上
        则不动, 避免打断手动切到的 feature 分支。
        """
        detached = (
            subprocess.run(
                ["git", "-C", path, "symbolic-ref", "-q", "HEAD"], capture_output=True, check=False
            ).returncode
            != 0
        )
        if not detached:
            return
        result = subprocess.run(
            ["git", "-C", path, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        branch = result.stdout.strip().rsplit("/", 1)[-1] if result.returncode == 0 else "master"
        subprocess.run(["git", "-C", path, "switch", branch], check=True)

    def build(self, message: str | None = None, version: str | None = None, *args, **kwargs) -> None:
        """递增共享版本号, 对每个 app submodule 转发同一版本号的 `funbuild build`,
        最后统一 push、打 tag 一次。

        参数:
            message: 透传给每个 app 及本仓库 `push` 的 commit 信息; 为 None 时各自走 aicommits。
            version: 显式指定的共享版本号, 透传给 `upgrade`; 为 None 时沿用旧逻辑自动递增。
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} build (submodule workspace)")
        self.pull()
        self.upgrade(version=version)
        for submodule_path in self._submodule_paths():
            if self._is_nested_workspace(submodule_path):
                logger.info(f"skip nested workspace, does not join the shared version: {submodule_path}")
                continue
            logger.info(f"build {submodule_path} with shared version {self.version}")
            self._switch_to_tracked_branch(submodule_path)
            subprocess.run(["git", "-C", submodule_path, "pull"], check=True)
            self._upgrade_pinned_packages(submodule_path)
            cmd = ["funbuild", "build", "--version", self.version]
            if message is not None:
                cmd.append(message)
            subprocess.run(cmd, cwd=submodule_path, check=True)
        self.push(message=message)
        self.tags()
