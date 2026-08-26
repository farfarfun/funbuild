#!/usr/bin/python3

import re
import shutil
import subprocess
from functools import lru_cache
from typing import Any, Optional

import tomlkit
from funshell import run_shell_list
from nltlog import getLogger

logger = getLogger("nltbuild")


class ShellCommandError(RuntimeError):
    """shell 命令链执行失败。"""


class NotAGitRepositoryError(RuntimeError):
    """当前目录不在 git 仓库中。"""


def load_toml(path: str) -> Any:
    """读取 TOML, 返回可像 dict 一样操作但保留原始排版的文档对象。

    必须用 tomlkit 而非 toml: 后者的 load/dump 往返会丢掉全部注释、把多行数组
    压成一行、并按字典顺序重排 table。upgrade 每次只改一个版本号, 却会因此重写
    整个 pyproject.toml, 既污染 diff 也会静默删除用户写的注释。
    """
    with open(path, encoding="utf-8") as f:
        return tomlkit.load(f)


def dump_toml(document: Any, path: str) -> None:
    """写回 TOML, 只有被修改的字段会变, 其余排版原样保留。"""
    with open(path, "w", encoding="utf-8") as f:
        tomlkit.dump(document, f)


def run_checked(commands: list[str], *, cwd: Optional[str] = None) -> None:
    """执行 shell 命令链, 任一条失败即抛出 ShellCommandError。

    funshell.run_shell_list(printf=True) 只把退出码当字符串返回、异常时返回
    "run shell error: ..." 且从不抛出, 直接调用会让构建/发布失败被静默忽略。
    """
    if not commands:
        return
    result = str(run_shell_list(commands, cwd=cwd)).strip()
    if result != "0":
        raise ShellCommandError(f"shell command chain failed (exit={result!r}): {' && '.join(commands)}")


# 形如 1、1.6、1.6.54、v1.6.54rc1 —— 取前导数字段, 其余作为后缀返回
_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*?)\s*$")


def parse_version(version: str) -> tuple[list[int], str]:
    """解析版本号为 [major, minor, patch] 与剩余后缀 (如 "rc1")。

    缺失的段补 0, 因此 "1" -> ([1, 0, 0], "")、"1.0" -> ([1, 0, 0], "")。
    无法解析前导数字时抛 ValueError。
    """
    match = _VERSION_RE.match(version or "")
    if not match:
        raise ValueError(f"cannot parse version: {version!r}")
    numbers = [int(match.group(index) or 0) for index in (1, 2, 3)]
    return numbers, match.group(4) or ""


@lru_cache(maxsize=1)
def _aicommits_available() -> bool:
    """aicommits 是否可用, 只探测一次 (push 会按批次调用多次)。"""
    if shutil.which("aicommits"):
        return True
    logger.warning("aicommits not found, fallback to default commit message")
    return False


def has_staged_changes(cwd=None) -> bool:
    """暂存区是否有待提交内容。"""
    return subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=cwd, check=False).returncode != 0


# 推理模型 (deepseek-reasoner、QwQ、R1 等) 会把思维链包在 <think> 里输出, aicommits
# 不做剥离就拿去提交, git 历史里于是出现整条正文只有 "<think>" 的提交。
_THINK_BLOCK_RE = re.compile(r"<\s*(think|thinking|reasoning)\s*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"<\s*/\s*(?:think|thinking|reasoning)\s*>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<\s*(?:think|thinking|reasoning)\s*>", re.IGNORECASE)
_FENCE_LINE_RE = re.compile(r"^\s*```.*$", re.MULTILINE)


def sanitize_commit_message(message: str) -> str:
    """剥掉思维链标记与 markdown 围栏, 返回可用作 commit 信息的正文。

    真正的结论通常在 </think> 之后; 若只剩下未闭合的 <think>, 说明输出被截断,
    整段都不可用, 返回空串由调用方决定回退。
    """
    text = _THINK_BLOCK_RE.sub("", message or "")
    # 落单的闭合标记: 结论在它之后
    closes = list(_THINK_CLOSE_RE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    # 落单的开启标记: 其后是被截断的思维链, 整段丢弃
    opens = _THINK_OPEN_RE.search(text)
    if opens:
        text = text[: opens.start()]
    text = _FENCE_LINE_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()


def _last_commit_message(cwd=None) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=cwd, check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def _repair_generated_message(cwd, fallback: str) -> None:
    """aicommits 刚提交完就检查信息, 含思维链标记则就地 amend 修正。"""
    original = _last_commit_message(cwd)
    cleaned = sanitize_commit_message(original)
    if cleaned == original.strip():
        return
    replacement = cleaned or fallback
    logger.warning(f"aicommits 生成的信息含推理标记 (模型可能是 reasoner), 已修正为: {replacement!r}")
    subprocess.run(["git", "commit", "--amend", "-m", replacement], cwd=cwd, check=True)


def aicommits_commit(cwd=None, fallback: str = "add") -> bool:
    """让 aicommits 依据暂存内容自行生成信息并提交, 成功返回 True。

    只在调用方没有指定 commit 信息时才该走这条路: aicommits 完全无视外部传入的
    信息, 用它提交等于丢弃用户显式给出的信息。
    """
    if not has_staged_changes(cwd):
        logger.warning("No staged changes")
        return False
    if not _aicommits_available():
        return False

    try:
        subprocess.run(["aicommits", "--yes"], cwd=cwd, check=True)
    except Exception as e:
        logger.error(f"aicommits commit failed: {e}")
        return False
    if has_staged_changes(cwd):
        return False
    _repair_generated_message(cwd, fallback)
    return True


def deep_get(data: dict, *args):
    if not data:
        return None
    for arg in args:
        if isinstance(arg, int) or arg in data:
            try:
                data = data[arg]
            except Exception as e:
                logger.debug(f"deep_get miss at {arg!r}: {e}")
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
