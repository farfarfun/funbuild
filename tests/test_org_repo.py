"""覆盖「这个仓库是否属于本组织」的判定。

以前用 `name.startswith("fun")`，组织经历 note* -> fun* -> 部分 nlt* 改名后
这个前缀不再代表归属：farlog / nltcache / nltspec / nltdeploy 是自有仓库却被
判成外部，于是拿不到许可证元数据、authors、urls，也不走 ruff 格式化。
"""

import subprocess

import pytest

from funbuild.core.base import is_org_repo


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/farfarfun/farlog.git",
        "https://github.com/farfarfun/nltcache",
        "git@github.com:farfarfun/nltspec.git",
        "https://github.com/FarFarFun/nltdeploy.git",  # 大小写不敏感
        "https://github.com/farfarfun/notebattle.git",  # 改名前的 note* 也算
    ],
)
def test_org_urls_recognised(repo, url):
    """自有仓库无论叫 fun* / nlt* / note*，都应判为组织内。"""
    _git(repo, "remote", "add", "origin", url)
    is_org_repo.cache_clear()
    assert is_org_repo(str(repo)) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/someone-else/funthing.git",  # 名字像但不是我们的
        "git@github.com:another-org/funtool.git",
        "https://gitlab.com/farfarfunny/x.git",
    ],
)
def test_foreign_urls_rejected(repo, url):
    """别人的仓库即使叫 fun* 也不能被套用组织约定。"""
    _git(repo, "remote", "add", "origin", url)
    is_org_repo.cache_clear()
    assert is_org_repo(str(repo)) is False


def test_no_remote_is_not_org(repo):
    """没有 remote 时不做断言性判断，交给调用方的前缀回退逻辑。"""
    is_org_repo.cache_clear()
    assert is_org_repo(str(repo)) is False


def test_not_a_git_dir(tmp_path):
    """非 git 目录不应抛异常。"""
    is_org_repo.cache_clear()
    assert is_org_repo(str(tmp_path / "nope")) is False
