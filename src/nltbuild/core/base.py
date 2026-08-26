#!/usr/bin/python3

import os
import subprocess
from functools import lru_cache

from funshell import run_shell

from .util import aicommits_commit, has_staged_changes, logger, parse_version, run_checked


@lru_cache(maxsize=8)
def _git_repo_root(cwd: str) -> str:
    """仓库根目录。

    registry 会依次实例化每个 builder 探测类型 (hybrid 还会再各建一个),
    单次 CLI 调用因此会重复执行同一条 git 命令 8 次。按 cwd 缓存: git 根只取决于
    当前目录, 同一目录下结果恒定, 而以 cwd 为键可保证 chdir 后不会读到旧值。
    """
    return run_shell("git rev-parse --show-toplevel", printf=False).strip()


class BaseBuild:
    """构建工具的基类"""

    def __init__(self, name=None):
        self.repo_path = _git_repo_root(os.getcwd())
        self.name = name or self.repo_path.split("/")[-1]
        self.version = None

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

    def upgrade(self, *args, **kwargs):
        """升级版本"""
        self.version = self.__version_upgrade()
        self._write_version()

    def pull(self, *args, **kwargs):
        """拉取代码"""
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

    def push(self, message=None, batch_size=20, *args, **kwargs):
        """推送代码。

        message 为 None 时交给 aicommits 依据改动自动生成信息; 显式传入则原样使用
        —— aicommits 会无视外部信息自己生成一条, 因此指定了信息就不能再走它。
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

    def install(self, *args, **kwargs):
        """安装包"""
        logger.info(f"{self.name} install")
        run_checked(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def build(self, message=None, *args, **kwargs):
        """构建发布流程"""
        logger.info(f"{self.name} build")
        self.pull()
        self.upgrade()
        run_checked(
            self._cmd_delete() + self._cmd_build() + self._cmd_install() + self._cmd_publish() + self._cmd_delete()
        )
        self.push(message=message)
        self.tags()

    def clean_history(self, *args, **kwargs):
        """清理git历史记录"""
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

    def clean(self, *args, **kwargs):
        """清理git缓存"""
        logger.info(f"{self.name} clean")
        run_checked(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore' || true",
                "git gc --aggressive",
            ]
        )

    def tags(self, *args, **kwargs):
        """创建版本标签"""
        if not self.version:
            logger.warning("skip tags: version is not set")
            return
        run_checked(
            [
                f"git tag --force v{self.version}",
                "git push --tags",
            ]
        )
