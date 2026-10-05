#!/usr/bin/python3

import os
import re
import shlex
import shutil
from functools import lru_cache
from typing import Any

import tomlkit
from farlog import getLogger
from funshell import run_shell, run_shell_list

logger = getLogger("funbuild")


class ShellCommandError(RuntimeError):
    """shell 命令链执行失败。"""


class NotAGitRepositoryError(RuntimeError):
    """当前目录不在 git 仓库中。"""


class ManifestParseError(RuntimeError):
    """版本清单文件 (pyproject.toml / package.json / pubspec.yaml 等) 无法读取或解析。"""


class BuilderDetectionError(RuntimeError):
    """构建类型探测失败: 清单文件损坏导致无法判定该用哪个构建策略。"""


# SPEC.md §10: 格式 `<类型>: <做了什么>`, 类型取下面这几个 ASCII 词。
COMMIT_MESSAGE_TYPES = ("feat", "fix", "docs", "refactor", "test", "chore")
DEFAULT_COMMIT_MESSAGE = "chore: 更新项目文件"
# 允许 conventional commits 的可选 scope (如 `fix(core): ...`)。描述不限语种:
# 曾要求必须含中文, 于是 aicommits 生成的英文信息一律被判非法, 每次发版都退回
# DEFAULT_COMMIT_MESSAGE, 历史里只剩一串「更新项目文件」。
_COMMIT_MESSAGE_RE = re.compile(rf"^(?:{'|'.join(COMMIT_MESSAGE_TYPES)})(?:\([^()\s]+\))?: \S.*$")
COMMIT_MESSAGE_HINT = (
    f"提交信息必须是 `<类型>: <描述>` 格式, 类型取 {'/'.join(COMMIT_MESSAGE_TYPES)}, 例如 {DEFAULT_COMMIT_MESSAGE!r}"
)


def is_valid_commit_message(message: str) -> bool:
    """检查提交标题是否为 `<类型>: <描述>` 格式。

    参数:
        message: 待检查的完整提交信息，首行作为标题校验。
    返回:
        标题符合组织约定 (类型取 COMMIT_MESSAGE_TYPES、描述非空) 时返回 True，
        否则返回 False。
    """
    lines = (message or "").splitlines()
    if not lines:
        return False
    subject = lines[0]
    return _COMMIT_MESSAGE_RE.fullmatch(subject) is not None


# cleanDirs 这类用户配置会被拼进 `rm -rf <值>` 并交给 shell 执行, 必须先过滤。
# 允许 glob (默认清理项本来就有 extbuild/*/dist), 但不允许任何能改变命令结构或
# 把删除范围带出仓库的字符。
_SHELL_METACHARS = set(";&|$`()<>\n\r\t\"'\\ ")


def safe_clean_dir(value: Any, *, source: str) -> str | None:
    """校验 `funbuild.cleanDirs` 里的一项, 通过则返回可直接拼进 `rm -rf` 的字符串。

    这些值来自 `package.json` / `pubspec.yaml`, 原先未经任何校验就 f-string 拼进
    `rm -rf {d}` 交给 shell: 带空格会被拆成多个删除目标, 带 `;` 或反引号即是命令
    注入, 写成 `/` 或 `../..` 则直接删到仓库外面去。

    参数:
        value: 配置里的原始项, 非字符串或空白一律丢弃。
        source: 配置来源 (文件路径或字段名), 仅用于告警信息定位。
    返回:
        合法时返回去空白后的相对路径 (可含 `*` `?` `[]` glob); 非法时返回 None,
        并记录一条带来源的告警。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    item = value.strip()
    if os.path.isabs(item) or item.startswith("~"):
        logger.warning(f"忽略 {source} 的 cleanDirs 项 {item!r}: 只允许仓库内的相对路径")
        return None
    if any(part == ".." for part in re.split(r"[\\/]+", item)):
        logger.warning(f"忽略 {source} 的 cleanDirs 项 {item!r}: 不允许 `..` 跳出仓库")
        return None
    if set(item) & _SHELL_METACHARS:
        logger.warning(f"忽略 {source} 的 cleanDirs 项 {item!r}: 含 shell 元字符或空白")
        return None
    return item


def normalize_package_name(name: str) -> str:
    """PEP 503 归一化: 大小写、`_`/`.`/`-` 的写法差异不应影响是否命中配置。

    npm/pub 的包名惯例本就是全小写, 借同一套归一化规则统一比对, 不需要
    再为每个生态单独维护一套大小写/分隔符规则。
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def load_toml(path: str) -> Any:
    """读取 TOML, 返回可像 dict 一样操作但保留原始排版的文档对象。

    必须用 tomlkit 而非 toml: 后者的 load/dump 往返会丢掉全部注释、把多行数组
    压成一行、并按字典顺序重排 table。upgrade 每次只改一个版本号, 却会因此重写
    整个 pyproject.toml, 既污染 diff 也会静默删除用户写的注释。

    参数:
        path: TOML 文件路径。
    返回:
        tomlkit 文档对象。
    异常:
        ManifestParseError: 文件读不出来或 TOML 语法有误。原始异常信息里不带
            文件路径, 多清单仓库 (extbuild/、exts/) 下无从定位是哪一个文件,
            这里统一补上。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return tomlkit.load(f)
    except ManifestParseError:
        raise
    except Exception as e:
        raise ManifestParseError(f"无法解析 TOML 文件 {path}: {e}") from e


def dump_toml(document: Any, path: str) -> None:
    """写回 TOML, 只有被修改的字段会变, 其余排版原样保留。"""
    with open(path, "w", encoding="utf-8") as f:
        tomlkit.dump(document, f)


def run_checked(commands: list[str], *, cwd: str | None = None) -> None:
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


def has_staged_changes(cwd: str | None = None) -> bool:
    """暂存区是否有待提交内容。"""
    status = run_shell("git diff --staged --quiet", cwd=cwd).strip()
    if status not in {"0", "1"}:
        raise ShellCommandError(f"git diff --staged --quiet failed (exit={status!r})")
    return status == "1"


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
    return run_shell("git log -1 --format=%B", printf=False, cwd=cwd)


# 标题已有的 `<类型>:` 前缀, 类型可能不在 COMMIT_MESSAGE_TYPES 里 (conventional
# 还有 perf / style / ci 等)
_SUBJECT_TYPE_RE = re.compile(r"^([A-Za-z]+)(?:\([^()\s]+\))?: (?=\S)")


def coerce_commit_message(message: str) -> str | None:
    """把信息补成合规格式; 没有任何可用描述时返回 None。

    aicommits 的 plain 模式 (`~/.aicommits` 里 `type=plain`, 也是它的默认值) 只输出
    纯描述、不带类型前缀, 于是每一条都判非法 —— 整条换成回退信息等于把描述丢掉,
    而缺的只是前缀。
    """
    if not message.strip():
        return None
    if is_valid_commit_message(message):
        return message
    subject, separator, body = message.partition("\n")
    # 表外类型整个换掉, 不叠成 `chore: perf: ...`
    match = _SUBJECT_TYPE_RE.match(subject)
    description = (subject[match.end() :] if match else subject).strip()
    if not description:
        return None
    # 不按描述猜类型: 让模型按 diff 判断才准, `aicommits config set type=conventional`
    return f"chore: {description}{separator}{body}"


def _repair_generated_message(cwd, fallback: str) -> None:
    """校验 aicommits 刚生成的信息，不合规时就地 amend 修正。"""
    original = _last_commit_message(cwd)
    cleaned = sanitize_commit_message(original)
    if cleaned == original.strip() and is_valid_commit_message(cleaned):
        return
    replacement = coerce_commit_message(cleaned) or fallback
    logger.warning(f"aicommits 生成的信息不符合提交规范, 已修正为: {replacement!r}")
    run_checked([shlex.join(["git", "commit", "--amend", "-m", replacement])], cwd=cwd)


def aicommits_commit(cwd=None, fallback: str = DEFAULT_COMMIT_MESSAGE) -> bool:
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
        run_checked(["aicommits --yes"], cwd=cwd)
    except Exception as e:
        logger.error(f"aicommits commit failed: {e}")
        return False
    if has_staged_changes(cwd):
        return False
    _repair_generated_message(cwd, fallback)
    return True


def deep_get(data: dict[str, Any], *args: str | int) -> Any:
    """按顺序逐层下钻取嵌套结构里的值, 任一层取不到就返回 None。

    用于读 `pyproject.toml` / `package.json` 这类层级不保证存在的配置,
    免去每层都写 `isinstance` + `in` 判断。

    参数:
        data: 起始容器, 通常是 TOML/JSON 解析结果; 为空 (None / 空 dict) 时直接返回 None。
        *args: 逐层的键 (dict 的 key) 或下标 (list 的 index), 按给定顺序依次下钻。
    返回:
        最后一层取到的值; 中途任一层缺失、类型不支持下标或下标越界时返回 None。
        注意取到的值本身就是 None 时同样返回 None, 无法与「缺失」区分。
    """
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


def deep_create(data: dict[str, Any], *args: str | int, key: str | int, value: Any) -> dict[str, Any]:
    """递归创建嵌套字典"""
    res = data
    for arg in args:
        if arg not in data:
            data[arg] = {}
        data = data[arg]
    data[key] = value
    return res
