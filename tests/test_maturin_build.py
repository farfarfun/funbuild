"""MaturinBuild 的行为测试。

重点钉三件事:

1. **探测优先级** —— maturin 项目必须被 MaturinBuild 认领, 不能被 UVBuild 抢走。
   抢走的后果是本地 `uv publish` 上传一个只对本机平台有效的 wheel。
2. **不在本地发布** —— `_cmd_publish()` 必须为空。
3. **版本号同步到 Cargo.toml** —— 且只改包自己的版本, 不碰依赖约束。
"""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funbuild.core import registry
from funbuild.core.base import git_repo_root
from funbuild.core.maturin_build import MaturinBuild
from funbuild.core.uv_build import UVBuild

MATURIN_PYPROJECT = """\
[build-system]
requires = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "demo"
version = "1.0.0"

[tool.maturin]
features = ["pyo3/abi3-py312"]
python-source = "python"
manifest-path = "Cargo.toml"
"""

PLAIN_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "demo"
version = "1.0.0"
"""

CARGO = """\
[workspace]
members = ["crates/core"]

[workspace.package]
version = "1.0.0"
edition = "2021"

[workspace.dependencies]
# 带注释, 用来确认同步版本号不会把文件重排
reqwest = { version = "0.12", default-features = false }
log = "0.4"

[package]
name = "demo-py"
version.workspace = true

[dependencies]
pyo3 = { version = "0.23", features = ["extension-module"] }
"""


def make_builder(version="2.0.0"):
    """绕过 __init__ 里的 git 调用。"""
    builder = MaturinBuild.__new__(MaturinBuild)
    builder.repo_path = os.getcwd()
    builder.name = "demo"
    builder.version = version
    builder.toml_paths = ["./pyproject.toml"]
    return builder


def not_org_repo():
    """`is_org_repo` 是只读 property, 且会起一个 git 子进程去问 remote。

    临时目录里没有 remote, 真跑一遍只是白等超时, 结果还取决于 self.name 的前缀。
    直接钉成 False, 让 `_cmd_build` 的断言只关心 maturin 那部分。
    """
    return patch.object(MaturinBuild, "is_org_repo", property(lambda self: False))


@contextlib.contextmanager
def project(files):
    """在临时目录里铺出一个项目并 chdir 进去 (探测全部走相对路径)。"""
    with tempfile.TemporaryDirectory() as temp:
        for name, content in files.items():
            path = Path(temp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(temp)
        git_repo_root.cache_clear()
        try:
            yield Path(temp)
        finally:
            os.chdir(cwd)
            git_repo_root.cache_clear()


class CheckTypeTest(unittest.TestCase):
    def test_claims_a_maturin_project(self):
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}):
            self.assertTrue(make_builder().check_type())

    def test_claims_project_declaring_only_the_tool_table(self):
        """只有 `[tool.maturin]`、build-backend 写别的 (例如本地改造中途) 也认。"""
        pyproject = PLAIN_PYPROJECT + '\n[tool.maturin]\nmodule-name = "demo._demo"\n'
        with project({"pyproject.toml": pyproject}):
            self.assertTrue(make_builder().check_type())

    def test_ignores_a_plain_python_project(self):
        with project({"pyproject.toml": PLAIN_PYPROJECT}):
            self.assertFalse(make_builder().check_type())

    def test_must_be_registered_before_uv_build(self):
        """UVBuild 同样会认领 maturin 项目 —— 所以顺序是唯一的保障。

        这条不是在测 UVBuild 的 bug, 而是把「注册顺序不能调换」这个约束写死:
        哪天有人按字母序重排 registry.py 的列表, 这里会红。
        """
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}):
            uv = UVBuild.__new__(UVBuild)
            uv.toml_paths = ["./pyproject.toml"]
            self.assertTrue(uv.check_type(), "前提: UVBuild 确实会认领 maturin 项目")

        # `builders` 是 get_build 里的局部变量, 拿不到对象, 只能断言源码顺序。
        source = Path(registry.__file__).read_text(encoding="utf-8")
        self.assertLess(
            source.index("MaturinBuild,"),
            source.index("UVBuild,"),
            "MaturinBuild 必须注册在 UVBuild 之前",
        )


class PublishTest(unittest.TestCase):
    def test_never_publishes_locally(self):
        """本地只能出一个平台的 wheel, 发布必须交给 tag 触发的 CI。"""
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}):
            self.assertEqual(make_builder()._cmd_publish(), [])


class BuildCommandTest(unittest.TestCase):
    def test_builds_with_maturin_into_the_shared_dist_dir(self):
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}) as root, not_org_repo():
            builder = make_builder()
            builder.repo_path = str(root)
            commands = builder._cmd_build()

        joined = "\n".join(commands)
        self.assertIn("maturin build --release", joined)
        # 产物落在 UVBuild 的 dist/funbuild/<key>/ 下, 这样继承来的 _cmd_install
        # (`uv pip install dist/funbuild/*/*.whl`) 不用改就能装上。
        self.assertIn(os.path.join("dist", "funbuild", "root"), joined)
        self.assertNotIn("uv build", joined)
        # Cargo.lock 要进仓库并由 CI 的 --locked 校验, 不能每次发版删掉重建。
        self.assertNotIn("uv lock", joined)

    def test_install_still_comes_from_uv_build(self):
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}):
            self.assertEqual(make_builder()._cmd_install(), ["uv pip install dist/funbuild/*/*.whl"])


class CargoVersionSyncTest(unittest.TestCase):
    def test_writes_the_package_version_only(self):
        with project({"pyproject.toml": MATURIN_PYPROJECT, "Cargo.toml": CARGO}) as root:
            make_builder("2.0.0")._write_version()
            cargo = (root / "Cargo.toml").read_text(encoding="utf-8")

        self.assertIn('version = "2.0.0"', cargo)
        self.assertNotIn('version = "1.0.0"', cargo)
        # 依赖约束不能跟着包版本走。
        self.assertIn('reqwest = { version = "0.12", default-features = false }', cargo)
        self.assertIn('pyo3 = { version = "0.23", features = ["extension-module"] }', cargo)
        self.assertIn('log = "0.4"', cargo)
        # 继承式声明保持原样。
        self.assertIn("version.workspace = true", cargo)
        # 注释与排版不被重写。
        self.assertIn("# 带注释, 用来确认同步版本号不会把文件重排", cargo)

    def test_honours_a_custom_manifest_path(self):
        pyproject = MATURIN_PYPROJECT.replace('manifest-path = "Cargo.toml"', 'manifest-path = "rust/Cargo.toml"')
        with project({"pyproject.toml": pyproject, "rust/Cargo.toml": CARGO}) as root:
            builder = make_builder("3.1.4")
            self.assertEqual(builder._cargo_manifest_paths(), ["rust/Cargo.toml"])
            builder._write_version()
            self.assertIn('version = "3.1.4"', (root / "rust" / "Cargo.toml").read_text(encoding="utf-8"))

    def test_missing_cargo_manifest_is_not_fatal(self):
        """Cargo.toml 还没建出来时 (改造进行中) 不该让 upgrade 整个失败。"""
        with project({"pyproject.toml": MATURIN_PYPROJECT}):
            self.assertEqual(make_builder()._cargo_manifest_paths(), [])
            make_builder()._write_version()  # 不抛异常即可


if __name__ == "__main__":
    unittest.main()
