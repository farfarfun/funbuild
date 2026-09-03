#!/usr/bin/python3

import json
import os
import re
from .util import dump_toml, load_toml, logger

# 形如 `version: 1.0.0+42`, 可能带引号与行内注释; 只替换 major.minor.patch 部分,
# 不动 `+42` 这类构建号后缀 —— 那是 FlutterBuild 自己在 upgrade 时才递增的字段,
# 被别的清单 (如根 pyproject.toml) 同步过来时不该被覆盖或清空。
_PUBSPEC_VERSION_RE = re.compile(r"^(version:\s*)(\S+)(.*)$", re.MULTILINE)


def replace_pubspec_version_line(raw: str, compute_new_token) -> tuple[str, int]:
    """替换 pubspec.yaml 里的 version 行, 保留引号风格与行内注释。

    compute_new_token(inner) 接收去掉引号后的原值, 返回新值 (同样不带引号)。
    """

    def repl(match):
        prefix, token, rest = match.group(1), match.group(2), match.group(3)
        quote = ""
        inner = token
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            quote = token[0]
            inner = token[1:-1]
        return f"{prefix}{quote}{compute_new_token(inner)}{quote}{rest}"

    return _PUBSPEC_VERSION_RE.subn(repl, raw, count=1)


def _iter_pyproject_toml_paths() -> list[str]:
    paths = ["./pyproject.toml"]
    for root in ("extbuild", "exts"):
        if os.path.isdir(root):
            for name in os.listdir(root):
                sub = os.path.join(root, name)
                if os.path.isdir(sub):
                    t = os.path.join(sub, "pyproject.toml")
                    if os.path.isfile(t):
                        paths.append(t)
    return paths


def _pyproject_supports_version_sync(path: str) -> bool:
    try:
        cfg = load_toml(path)
    except Exception:
        return False
    proj = cfg.get("project")
    if isinstance(proj, dict) and "version" in proj:
        return True
    tool = cfg.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and "version" in poetry:
            return True
    return False


def _collect_pyproject_paths_for_version_sync() -> list[str]:
    return [p for p in _iter_pyproject_toml_paths() if _pyproject_supports_version_sync(p)]


def _append_package_json_if_versioned(path: str, out: list[str]) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    ver = pkg.get("version")
    if isinstance(ver, str) and ver.strip():
        out.append(path)


def _collect_package_json_paths_for_version_sync() -> list[str]:
    paths: list[str] = []
    _append_package_json_if_versioned("./package.json", paths)
    for root in ("extbuild", "exts"):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            sub = os.path.join(root, name)
            if not os.path.isdir(sub):
                continue
            _append_package_json_if_versioned(os.path.join(sub, "package.json"), paths)
    return paths


def _append_pubspec_if_versioned(path: str, out: list[str]) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return
    if _PUBSPEC_VERSION_RE.search(raw):
        out.append(path)


def _collect_pubspec_paths_for_version_sync() -> list[str]:
    paths: list[str] = []
    _append_pubspec_if_versioned("./pubspec.yaml", paths)
    for root in ("extbuild", "exts"):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            sub = os.path.join(root, name)
            if os.path.isdir(sub):
                _append_pubspec_if_versioned(os.path.join(sub, "pubspec.yaml"), paths)
    return paths


def _sync_pubspec_version_file(path: str, version: str) -> None:
    """同步 major.minor.patch, 保留原有的 `+buildNumber` 构建号不变。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    new_raw, count = replace_pubspec_version_line(
        raw, lambda inner: version if "+" not in inner else f"{version}+{inner.split('+', 1)[1]}"
    )
    if count == 0:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_raw)


def _sync_pyproject_version_file(path: str, version: str) -> None:
    config = load_toml(path)
    changed = False
    proj = config.get("project")
    if isinstance(proj, dict) and "version" in proj:
        config["project"]["version"] = version
        changed = True
    tool = config.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and "version" in poetry:
            poetry["version"] = version
            changed = True
    if not changed:
        return
    dump_toml(config, path)


def _sync_package_json_version_file(path: str, version: str) -> None:
    with open(path, encoding="utf-8") as f:
        pkg = json.load(f)
    pkg["version"] = version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def root_pyproject_project_version() -> str | None:
    """根目录 pyproject.toml 中 [project].version, 作为整仓版本主源。"""
    path = "./pyproject.toml"
    if not os.path.isfile(path):
        return None
    try:
        cfg = load_toml(path)
    except Exception:
        return None
    proj = cfg.get("project")
    if not isinstance(proj, dict):
        return None
    v = proj.get("version")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def sync_all_manifest_versions(version: str) -> None:
    """将同一版本写入仓库内所有带 version 字段的 pyproject.toml、package.json 与
    pubspec.yaml (含 extbuild/exts 子目录)。pubspec.yaml 只同步 major.minor.patch,
    其 `+buildNumber` 后缀保持不变。"""
    v = version.strip() if isinstance(version, str) else str(version)
    for path in _collect_package_json_paths_for_version_sync():
        try:
            _sync_package_json_version_file(path, v)
        except (OSError, json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"sync package.json version skipped {path}: {e}")
    for path in _collect_pyproject_paths_for_version_sync():
        try:
            _sync_pyproject_version_file(path, v)
        except Exception as e:
            logger.warning(f"sync pyproject version skipped {path}: {e}")
    for path in _collect_pubspec_paths_for_version_sync():
        try:
            _sync_pubspec_version_file(path, v)
        except OSError as e:
            logger.warning(f"sync pubspec.yaml version skipped {path}: {e}")
