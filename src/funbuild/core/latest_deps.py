#!/usr/bin/python3

"""发版时把指定依赖的版本下界抬到「此刻最新的已发布版本」。

动机: 组织内部包之间 (funflix-api -> funflix) 希望每次发版都带上最新的上游。
`UVBuild._cmd_build` 里的 `rm -rf uv.lock && uv lock` 只影响本仓库构建时解析到
的版本, 发布出去的 wheel metadata 依然写着 pyproject 里那个早已过期的下界 ——
下游 `pip install -U funflix-api` 默认 `--upgrade-strategy only-if-needed`, 已装
的旧 funflix 满足旧下界就不会被升上来。要真的「每次都最新」, 必须把下界本身写回
pyproject.toml, 让它进 metadata。

配置见 `latest_packages()`。只处理 PEP 621 形状的依赖声明 (`[project]`
`dependencies` / `optional-dependencies`, 以及 PEP 735 的 `[dependency-groups]`);
Poetry 的 `[tool.poetry.dependencies]` 表形式不在覆盖范围内。
"""

import os
import re
import shlex
import tempfile
from typing import Any

import tomlkit
from funshell import run_shell

from .util import deep_get, dump_toml, load_toml, logger, normalize_package_name

# 独立仓库把配置写在自己的 pyproject.toml 里
PYPROJECT_CONFIG_KEY = "latest-packages"
# `<product>-dev` 编排仓库沿用 SubmoduleWorkspaceBuild 已有的那份配置, 不必写两遍
WORKSPACE_CONFIG_PATH = "scripts/funbuild.toml"
WORKSPACE_CONFIG_KEY = "packages"


class LatestDependencyError(RuntimeError):
    """声明了「每次发版取最新」, 却没能确定最新版本。

    不能降级成警告: 配置摆在那里却静默沿用旧下界, 等于发出去一个看着正常、
    实际钉着过期上游的包, 而这恰恰是这个功能要消灭的 bug。
    """


# PEP 508 的 `name[extras] <specifiers>` 开头部分; rest 留给调用方继续拆
_REQUIREMENT_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?(?P<rest>.*)$", re.DOTALL)
# 版本约束的单个子句, 如 ">=1.0.0"、"!= 1.2.*"
_CLAUSE_RE = re.compile(r"^\s*(===|==|!=|<=|>=|~=|<|>)\s*(.+?)\s*$")
# `uv pip compile` 输出的锁定行, 如 "funflix==1.9.0"
_PINNED_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#]+)")
# 这几个运算符表达的是「从哪个版本起」, 抬最新版时要跟着走。`<`/`<=`/`!=`/`===`
# 表达的是上界与排除项 —— 那是调用方刻意加的限制, 原样保留, 不替他做决定。
_LOWER_BOUND_OPERATORS = ("~=", ">=", ">", "==")


def _read_string_list(path: str, *keys: str) -> list[str]:
    """读 TOML 里某个字符串数组; 文件不存在或字段缺失时返回空列表。

    解析失败只警告不抛: 配置文件读不出来时, 该不该中止发布由调用方按「有没有
    声明过 latest-packages」判断, 而这里恰恰是连有没有声明都不知道。
    """
    if not os.path.isfile(path):
        return []
    try:
        data = load_toml(path)
    except Exception as e:
        logger.warning(f"skip {path}: {e}")
        return []
    value = deep_get(data, *keys)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def pyproject_latest_packages(repo_path: str) -> list[str]:
    """只取根 `pyproject.toml` 里声明的那一份名单。

    与 `latest_packages()` 的区别是不含 `scripts/funbuild.toml` 的 `packages`:
    那是编排仓库「这条链上要发哪些包」的清单, 不是「本仓库依赖哪些包」。缺失的
    依赖只按这一份名单补齐 —— 拿整链清单去补, 会给编排仓库的根 pyproject 凭空
    加上一堆它并不依赖的包。
    """
    return _read_string_list(os.path.join(repo_path, "pyproject.toml"), "tool", "funbuild", PYPROJECT_CONFIG_KEY)


def latest_packages(repo_path: str) -> list[str]:
    """声明了「发版时抬到最新」的包名列表, 两个来源取并集。

    - 根 `pyproject.toml` 的 `[tool.funbuild].latest-packages` —— 独立仓库用这个:

          [tool.funbuild]
          latest-packages = ["funflix"]

    - `scripts/funbuild.toml` 的 `packages` —— `<product>-dev` 编排仓库已有的
      约定 (见 SubmoduleWorkspaceBuild), 在那类仓库里直接复用, 不必再写一遍。

    参数:
        repo_path: 仓库根目录。
    返回:
        去重后的包名列表, 保持首次出现的顺序。
    """
    names = pyproject_latest_packages(repo_path)
    names += _read_string_list(os.path.join(repo_path, WORKSPACE_CONFIG_PATH), WORKSPACE_CONFIG_KEY)
    return list(dict.fromkeys(names))


def resolve_latest_version(package: str, cwd: str) -> str:
    """问 index 要 `package` 当前的最新版本号。

    用 `uv pip compile` 而不是 PyPI 的 JSON API: 它会读 `cwd` 下 pyproject.toml 的
    `[tool.uv]` 配置, 所以组织内部包发在私有 index 上时同样解析得到 —— 写死
    pypi.org 的 URL 则一律 404。`--no-deps` 让它只解析这一个包, 不会被不相干的
    传递依赖冲突拖垮, 也快得多。

    参数:
        package: 包名, 可带 extras 之外的任意大小写/分隔符写法。
        cwd: 解析时的工作目录, 用于读取仓库自己的 uv 配置 (index 等)。
    返回:
        形如 "1.9.0" 的版本号。
    异常:
        LatestDependencyError: 解析不出该包的版本时抛出。
    """
    # 不走 `printf ... | uv pip compile -`: 命令要经 shell=True 拼接,
    # 临时文件省掉一层引号转义, 也便于把失败信息原样带进异常。
    handle, requirement_path = tempfile.mkstemp(suffix=".in", prefix="funbuild-latest-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(f"{package}\n")
        command = shlex.join(
            ["uv", "pip", "compile", "--no-header", "--no-deps", "--prerelease=allow", requirement_path]
        )
        # printf=False 才能拿到 stdout; 失败时 uv 把原因写 stderr, stdout 为空,
        # 于是下面匹配不到锁定行, 统一走 LatestDependencyError。
        output = run_shell(command, printf=False, cwd=cwd, timeout=300)
    finally:
        os.unlink(requirement_path)

    normalized = normalize_package_name(package)
    for line in output.splitlines():
        match = _PINNED_RE.match(line.strip())
        if match and normalize_package_name(match.group("name")) == normalized:
            return match.group("version")
    raise LatestDependencyError(f"无法解析 {package!r} 的最新版本, 拒绝带着过期的依赖下界继续发布: {command}")


def rewrite_requirement(requirement: str, version: str) -> str | None:
    """把一条依赖声明里的版本下界改写成 `version`; 无需改动时返回 None。

    extras、environment marker、上界与排除项都原样保留: 最新版为 1.9.0 时
    `funflix[all]>=1.0,<2; python_version>="3.11"` 变成
    `funflix[all]>=1.9.0,<2; python_version>="3.11"`。原本没写任何版本约束的
    `funflix` 会补上 `>=1.9.0`。

    `funflix @ https://...` 这类 PEP 508 直接引用的版本不来自 index, 返回 None
    原样留着 —— 那是调用方显式指定的来源, 不该被悄悄换掉。

    参数:
        requirement: 一条 PEP 508 依赖声明。
        version: 要写入的版本号。
    返回:
        改写后的声明; 解析不了、是直接引用、或改写结果与原文一致时返回 None。
    """
    head, separator, marker = requirement.partition(";")
    match = _REQUIREMENT_RE.match(head.strip())
    if not match:
        return None
    rest = match.group("rest").strip()
    if rest.startswith("@"):
        return None
    # PEP 508 允许 `name (>=1.0)` 的带括号写法, 统一剥掉再按子句处理
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()

    rendered: list[str] = []
    replaced = False
    for clause in rest.split(","):
        if not clause.strip():
            continue
        clause_match = _CLAUSE_RE.match(clause)
        if clause_match and clause_match.group(1) in _LOWER_BOUND_OPERATORS:
            rendered.append(f"{clause_match.group(1)}{version}")
            replaced = True
        else:
            rendered.append(clause.strip())
    if not replaced:
        rendered.insert(0, f">={version}")
    # 原文写了多个下界子句 (`>=1.0,>=1.2`) 时, 替换后会得到两条一模一样的约束
    rendered = list(dict.fromkeys(rendered))

    head = f"{match.group('name')}{match.group('extras') or ''}{','.join(rendered)}"
    updated = f"{head};{marker}" if separator else head
    return None if updated == requirement else updated


def _rewrite_dependency_array(dependencies, targets: dict[str, str], declared: set[str]) -> bool:
    """原地改写一个依赖数组, 有改动返回 True; 命中的包名记入 `declared`。

    逐项赋值而非整体替换数组: tomlkit 的 `Array.__setitem__` 会保留该行原有的
    缩进与行内注释, 整体赋值则会把用户写在数组里的注释和换行风格一并冲掉。

    `declared` 收的是「这个包已经在某个依赖数组里出现过」, 与是否真的改写无关 ——
    已经是最新版、或是 `name @ url` 这种不改动的形式, 同样算声明过, 不该再被
    `_add_missing_dependencies` 补上第二份。
    """
    if not isinstance(dependencies, list):
        return False
    changed = False
    for index, dependency in enumerate(list(dependencies)):
        # `[dependency-groups]` 里可以出现 `{include-group = "..."}` 这种表项
        if not isinstance(dependency, str):
            continue
        match = _REQUIREMENT_RE.match(dependency.strip())
        if not match:
            continue
        name = normalize_package_name(match.group("name"))
        version = targets.get(name)
        if not version:
            continue
        declared.add(name)
        updated = rewrite_requirement(dependency.strip(), version)
        if updated is None:
            continue
        logger.info(f"latest-packages: {dependency.strip()} -> {updated}")
        dependencies[index] = updated
        changed = True
    return changed


def _rewrite_document(config: Any, targets: dict[str, str], declared: set[str]) -> bool:
    """改写一份已解析的 pyproject 文档里命中的依赖下界, 有改动返回 True。"""
    changed = _rewrite_dependency_array(deep_get(config, "project", "dependencies"), targets, declared)
    for table in (deep_get(config, "project", "optional-dependencies"), config.get("dependency-groups")):
        if isinstance(table, dict):
            for group in table.values():
                changed = _rewrite_dependency_array(group, targets, declared) or changed
    return changed


def _add_missing_dependencies(config: Any, missing: dict[str, str], toml_path: str) -> bool:
    """把名单里声明了、却压根没写进依赖的包补进 `[project].dependencies`。

    不补的话这条配置等于白写: 包不在依赖里, 既进不了 wheel metadata, 也不会被
    `uv lock` 解析 —— 而「在 funflix-api 里写上 latest-packages = ["funflix"],
    此后每次发版都带最新的 funflix」正是这个功能的全部意义。

    参数:
        config: 根 pyproject 的文档对象, 原地修改。
        missing: 包名 (保持配置里的原始写法) -> 要写入的最新版本号。
        toml_path: 该文档的路径, 仅用于日志定位。
    返回:
        有补上任何一项时返回 True。
    """
    project = deep_get(config, "project")
    if not isinstance(project, dict):
        # 编排仓库的根 pyproject 可能根本不是一个 PEP 621 包。凭空造出 [project]
        # 只会生成一份缺 name/version 的残缺元数据, 不如显式告警交给人处理。
        logger.warning(f"latest-packages: {toml_path} 没有 [project] 表, 无法补齐 {sorted(missing)}")
        return False

    own = normalize_package_name(project.get("name") or "")
    dependencies = project.get("dependencies")
    if dependencies is None:
        dependencies = tomlkit.array()
        project["dependencies"] = dependencies
    elif not isinstance(dependencies, list):
        logger.warning(f"latest-packages: {toml_path} 的 [project].dependencies 不是数组, 无法补齐 {sorted(missing)}")
        return False

    changed = False
    for package, version in missing.items():
        if own and normalize_package_name(package) == own:
            # 自己依赖自己会让 uv 直接解析失败。编排仓库沿用 scripts/funbuild.toml
            # 的整链名单时, 本仓库自己的包名必然在名单里, 这条不是边角情况。
            logger.info(f"latest-packages: 跳过 {package}, 它就是 {toml_path} 自己的包")
            continue
        requirement = f"{package}>={version}"
        logger.info(f"latest-packages: {toml_path} 的 dependencies 里没有 {package}, 补上 {requirement}")
        dependencies.append(requirement)
        changed = True
    return changed


def _load_pyproject(toml_path: str) -> Any:
    """读一份 pyproject; 解析失败即中止发布, 不沿用旧下界。"""
    try:
        return load_toml(toml_path)
    except Exception as e:
        raise LatestDependencyError(f"无法解析 {toml_path}, 拒绝带着过期的依赖下界继续发布: {e}") from e


def sync_latest_dependencies(repo_path: str, toml_paths: list[str]) -> list[str]:
    """把配置里声明的包, 在各 pyproject.toml 中的版本下界抬到最新已发布版本。

    名单里的包若在任何依赖数组里都没出现过, 会按最新版本补进根 pyproject 的
    `[project].dependencies` (见 `_add_missing_dependencies`)。

    没有任何配置时直接返回, 不发起任何网络请求 —— 绝大多数仓库都走这条路径。

    参数:
        repo_path: 仓库根目录, 同时作为解析最新版本时的工作目录。
        toml_paths: 待改写的 pyproject.toml 路径列表 (通常是 `UVBuild.toml_paths`)。
    返回:
        实际被改写的文件路径列表。
    异常:
        LatestDependencyError: 配置里的包解析不出最新版本, 或 pyproject 解析失败。
    """
    packages = latest_packages(repo_path)
    if not packages:
        return []

    targets: dict[str, str] = {}
    for package in packages:
        version = resolve_latest_version(package, repo_path)
        logger.info(f"latest-packages: 解析到 {package} 的最新版本 {version}")
        targets[normalize_package_name(package)] = version

    # 先全部读进来再统一落盘: 「有没有声明过」要看齐所有清单 (包在子包里声明了
    # 就不该再往根上补一份), 而补齐又要改根文档 —— 边读边写会让根文件被写两次,
    # 第二次还是改在一份过期的文档上。按 realpath 去重, 因为 toml_paths[0] 通常
    # 是相对路径 "./pyproject.toml", 与下面拼出来的根路径指向同一个文件。
    root_path = os.path.join(repo_path, "pyproject.toml")
    documents: dict[str, tuple[str, Any]] = {}
    for path in toml_paths:
        key = os.path.realpath(path)
        if key not in documents:
            documents[key] = (path, _load_pyproject(path))
    root_key = os.path.realpath(root_path)
    if root_key not in documents and os.path.isfile(root_path):
        documents[root_key] = (root_path, _load_pyproject(root_path))

    declared: set[str] = set()
    changed = {key: _rewrite_document(config, targets, declared) for key, (_, config) in documents.items()}

    missing = {
        package: targets[normalize_package_name(package)]
        for package in pyproject_latest_packages(repo_path)
        if normalize_package_name(package) not in declared
    }
    if missing and root_key in documents:
        path, config = documents[root_key]
        changed[root_key] = _add_missing_dependencies(config, missing, path) or changed[root_key]
    elif missing:
        logger.warning(f"latest-packages: 找不到 {root_path}, 无法补齐 {sorted(missing)}")

    result = []
    for key, (path, config) in documents.items():
        if changed[key]:
            dump_toml(config, path)
            result.append(path)
    return result
