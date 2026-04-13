#!/usr/bin/python3

import json
import os
from typing import Optional

import toml

from .util import logger


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
        cfg = toml.load(path)
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


def _sync_pyproject_version_file(path: str, version: str) -> None:
    config = toml.load(path)
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
    with open(path, "w", encoding="utf-8") as f:
        toml.dump(config, f)


def _sync_package_json_version_file(path: str, version: str) -> None:
    with open(path, encoding="utf-8") as f:
        pkg = json.load(f)
    pkg["version"] = version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def root_pyproject_project_version() -> Optional[str]:
    """根目录 pyproject.toml 中 [project].version, 作为整仓版本主源。"""
    path = "./pyproject.toml"
    if not os.path.isfile(path):
        return None
    try:
        cfg = toml.load(path)
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
    """将同一版本写入仓库内所有带 version 字段的 pyproject.toml 与 package.json (含 extbuild/exts 子目录)。"""
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
