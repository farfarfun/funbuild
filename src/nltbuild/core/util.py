#!/usr/bin/python3

import subprocess

from nltlog import getLogger

logger = getLogger("nltbuild")


def opencommit_commit(default_message: str = "add", cwd=None) -> bool:
    """使用 opencommit CLI 自动提交, 成功返回 True。"""
    try:
        if subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=cwd, check=False).returncode == 0:
            logger.warning("No staged changes")
            return False

        subprocess.run(["aicommits", "--yes"], cwd=cwd, check=True)
        return subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=cwd, check=False).returncode == 0
    except Exception as e:
        logger.error(f"opencommit commit failed: {e}")
        logger.info(f"fallback to default commit message: {default_message}")
        return False


def deep_get(data: dict, *args):
    if not data:
        return None
    for arg in args:
        if isinstance(arg, int) or arg in data:
            try:
                data = data[arg]
            except Exception as e:
                print(e)
                return None
        else:
            return None
    return data


def deep_create(data, *args, key, value):
    """递归创建嵌套字典"""
    res = data
    for arg in args:
        if arg not in data:
            data[arg] = {}
        data = data[arg]
    data[key] = value
    return res
