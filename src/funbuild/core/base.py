#!/usr/bin/python3

import os
import shlex
import subprocess
from functools import lru_cache

from funshell import run_shell

from .util import (
    NotAGitRepositoryError,
    aicommits_commit,
    has_staged_changes,
    logger,
    parse_version,
    run_checked,
)


@lru_cache(maxsize=8)
def git_repo_root(cwd: str) -> str:
    """仓库根目录。

    registry 会依次实例化每个 builder 探测类型 (hybrid 还会再各建一个),
    单次 CLI 调用因此会重复执行同一条 git 命令 8 次。按 cwd 缓存: git 根只取决于
    当前目录, 同一目录下结果恒定, 而以 cwd 为键可保证 chdir 后不会读到旧值。
    """
    root = run_shell("git rev-parse --show-toplevel", printf=False).strip()
    # funshell 在 git 失败时返回空串而非抛异常。放任它会得到 repo_path='',
    # 直到后面 subprocess(cwd='') 抛出 FileNotFoundError: '' —— 完全看不出
    # 真正的原因是「这儿不是 git 仓库」。
    if not root or not os.path.isdir(root):
        raise NotAGitRepositoryError(f"当前目录不在 git 仓库中: {cwd}")
    return root


@lru_cache(maxsize=8)
def is_org_repo(repo_path: str, org: str = "farfarfun") -> bool:
    """这个仓库是否属于本组织。

    以前用 `name.startswith("fun")` 判断, 但组织经历过 note* -> fun* -> 部分 nlt*
    的改名, 前缀早就不能代表归属: farlog / nltcache / nltspec / nltdeploy 都是自有
    仓库却被判成外部, 于是拿不到许可证元数据、authors、urls, 也不走 ruff 格式化。
    真正要问的是 remote 指向谁, 就直接问 remote。
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    if out.returncode != 0:
        return False
    url = out.stdout.strip().lower()
    # 同时匹配 https://github.com/<org>/x 与 git@github.com:<org>/x
    return f"/{org}/" in url or f":{org}/" in url


class BaseBuild:
    """构建工具的基类"""

    def __init__(self, name: str | None = None) -> None:
        """初始化构建器。

        参数:
            name: 包名。为 None 时取当前 git 仓库根目录名作为默认包名。
        返回:
            无。
        """
        self.repo_path = git_repo_root(os.getcwd())
        self.name = name or self.repo_path.split("/")[-1]
        self.version = None

    @property
    def is_org_repo(self) -> bool:
        """仓库归属本组织时, 才自动套用组织约定 (许可证 / authors / urls / ruff)。"""
        if is_org_repo(self.repo_path):
            return True
        # remote 缺失或临时不可读时退回旧的前缀判断, 保证离线也能工作
        return self.name.startswith(("fun", "nlt", "note"))

    def check_type(self) -> bool:
        """检查是否为当前构建类型"""
        raise NotImplementedError

    def _write_version(self):
        """写入版本号"""
        raise NotImplementedError

    def __version_upgrade(self, step=128):
        """版本号自增: 按 step 进制进位, 即 patch 满 step 时向 minor 进位。"""
        version = self.version or "0.0.1"

        parts, suffix = parse_version(version)
        if suffix:
            logger.warning(f"version {version!r} has suffix {suffix!r}, dropped when upgrading")

        total = parts[0] * step * step + parts[1] * step + parts[2] + 1
        return f"{total // (step * step)}.{total // step % step}.{total % step}"

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return []

    def _cmd_publish(self) -> list[str]:
        """发布命令"""
        return []

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return ["pip install dist/*.whl --force-reinstall"]

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return [
            "rm -rf dist",
            "rm -rf extbuild/*/dist",
            "rm -rf build",
            "rm -rf extbuild/*/build",
            "rm -rf *.egg-info",
            "rm -rf extbuild/*/src/*.egg-info",
            "rm -rf uv.lock",
        ]

    def upgrade(self, *args, **kwargs) -> None:
        """升级版本号并写回配置文件。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        self.version = self.__version_upgrade()
        self._write_version()

    def pull(self, *args, **kwargs) -> None:
        """从远端拉取最新代码 (`git pull`)。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} pull")
        run_checked(["git pull"])

    def _changed_files(self):
        output = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.repo_path,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        fields = output.split(b"\0")
        changes = []
        index = 0
        while index < len(fields) and fields[index]:
            record = fields[index]
            paths = [os.fsdecode(record[3:])]
            if b"R" in record[:2] or b"C" in record[:2]:
                index += 1
                paths.append(os.fsdecode(fields[index]))
            try:
                modified = os.lstat(os.path.join(self.repo_path, paths[0])).st_mtime_ns
            except FileNotFoundError:
                modified = 0
            changes.append((modified, paths[0], paths))
            index += 1
        return sorted(changes)

    def push(self, message: str | None = None, batch_size: int = 20, *args, **kwargs) -> None:
        """把改动分批提交并推送到远端。

        message 为 None 时交给 aicommits 依据改动自动生成信息; 显式传入则原样使用
        —— aicommits 会无视外部信息自己生成一条, 因此指定了信息就不能再走它。

        参数:
            message: 提交信息。为 None 时优先尝试 aicommits 自动生成, 失败则用 "add"。
            batch_size: 每次提交最多包含的文件数, 用于避免单次提交内容过大。
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        异常:
            ValueError: batch_size 小于 1 时抛出。
        """
        logger.info(f"{self.name} push")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        changes = self._changed_files()
        if changes:
            subprocess.run(["git", "reset", "--quiet"], cwd=self.repo_path, check=True)
        for start in range(0, len(changes), batch_size):
            paths = list(dict.fromkeys(path for change in changes[start : start + batch_size] for path in change[2]))
            subprocess.run(["git", "add", "-A", "-f", "--", *paths], cwd=self.repo_path, check=True)
            # 本批内容可能已被上一次提交带走 (如 aicommits 提交了全部暂存内容),
            # 此时 git commit 会因无内容可提交而失败, 直接跳过。
            if not has_staged_changes(self.repo_path):
                continue
            if message is None and aicommits_commit(cwd=self.repo_path):
                continue
            subprocess.run(["git", "commit", "-m", message or "add"], cwd=self.repo_path, check=True)
        subprocess.run(["git", "push"], cwd=self.repo_path, check=True)

    def _submodule_paths(self) -> list[str]:
        """当前仓库已初始化的直接 submodule 绝对路径列表。

        未初始化的 submodule 目录里没有 .git, 没法 push, 过滤掉 (`git submodule
        status` 中以 "-" 打头的行)。无 submodule 或非 git 仓库时返回空列表。
        """
        result = subprocess.run(
            ["git", "submodule", "status"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        paths = []
        for line in result.stdout.splitlines():
            if not line or line[0] == "-":
                continue
            fields = line.strip().split()
            if len(fields) >= 2:
                paths.append(os.path.join(self.repo_path, fields[1]))
        return paths

    def push_all(self, message: str | None = None, batch_size: int = 20, *args, **kwargs) -> None:
        """先在每个 submodule 里执行一次 `funbuild push`, 再 push 当前仓库。

        submodule 通过子进程调用 `funbuild push`(而非直接复用 self.push()), 使其
        行为与用户手动 cd 进去执行完全一致 —— 包括各 submodule 自身的构建类型探测。
        不递归到 submodule 的 submodule, 与不带 "all" 时的 push 语义保持一致。

        参数:
            message: 透传给每一层 push 的提交信息, 为 None 时各层各自走 aicommits。
            batch_size: 透传给每一层 push 的批次大小。
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        for submodule_path in self._submodule_paths():
            logger.info(f"push submodule: {submodule_path}")
            cmd = ["funbuild", "push"]
            if message is not None:
                cmd += ["--message", message]
            cmd += ["--batch-size", str(batch_size)]
            subprocess.run(cmd, cwd=submodule_path, check=True)
        self.push(message=message, batch_size=batch_size)

    def install(self, *args, **kwargs) -> None:
        """本地构建并安装, 用于开发环境验证当前代码可安装, 不发布也不打标签。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} install")
        run_checked(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def build(self, message: str | None = None, *args, **kwargs) -> None:
        """完整发布流程: pull -> upgrade -> 清理 -> 构建 -> 安装校验 -> 发布 -> 清理 -> push -> tag。

        任一步失败会立即中止 (由 run_checked 抛出异常), 不会继续 push 或打标签。

        参数:
            message: 透传给 `push` 的提交信息, 为 None 时走 aicommits 自动生成。
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} build")
        self.pull()
        self.upgrade()
        run_checked(
            self._cmd_delete() + self._cmd_build() + self._cmd_install() + self._cmd_publish() + self._cmd_delete()
        )
        self.push(message=message)
        self.tags()

    def clean_history(self, *args, **kwargs) -> None:
        """抹掉全部 git 历史与标签并强推当前分支, 不可恢复且无二次确认。

        高风险操作: 会删除本地及远端所有 tag, 并用一个 orphan 分支替换当前
        分支的全部历史, 最后强制推送覆盖远端。仅用于确需清空历史的场景。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} clean history")
        current_branch = run_shell("git rev-parse --abbrev-ref HEAD", printf=False).strip() or "master"
        run_checked(
            [
                "git tag -d $(git tag -l) || true",
                "git fetch",
                # 无 tag 时 `git push origin --delete` 会因缺少参数报错, 故先判空
                '[ -z "$(git tag -l)" ] || git push origin --delete $(git tag -l)',
                "git tag -d $(git tag -l) || true",
                "git checkout --orphan latest_branch",
                "git add -A",
                'git commit -am "clear history"',
                f"git branch -D {current_branch} || true",
                f"git branch -m {current_branch}",
                f"git push -f origin {current_branch}",
                f"git push --set-upstream origin {current_branch}",
                f"echo {self.name} success",
            ]
        )

    def clean(self, *args, **kwargs) -> None:
        """清理 git 索引缓存并重新提交, 使新增的 .gitignore 规则对已跟踪文件生效。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        logger.info(f"{self.name} clean")
        run_checked(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore' || true",
                "git gc --aggressive",
            ]
        )

    def tags(self, *args, **kwargs) -> None:
        """为当前版本号打 `v<version>` 标签并强制推送到远端。

        `self.version` 未设置(如未走过 `upgrade`)时跳过并记录警告, 不报错。

        参数:
            *args, **kwargs: 由 CLI 透传, 当前实现未使用, 仅为接口一致性保留。
        返回:
            无。
        """
        if not self.version:
            logger.warning("skip tags: version is not set")
            return
        tag = shlex.quote(f"v{self.version}")
        run_checked(
            [
                f"git tag --force {tag}",
                # 只推这一个 tag, 且强制覆盖。`git push --tags` 有两个毛病:
                # 会把本地所有 tag 一并推上去; 且拒绝更新远端已存在的 tag ——
                # 与上一行 `git tag --force` 的意图直接矛盾, 重发同一版本时会在
                # 发布已经成功之后卡在这里报错。
                f"git push --force origin {tag}",
            ]
        )
