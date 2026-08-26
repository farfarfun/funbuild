"""覆盖 PEP 639 许可证元数据的写入。

回归背景：`_apply_license_metadata` 会写 `project.license = "MIT"`（SPDX 表达式），
但一度没有清掉 `License ::` classifier。两者共存时 setuptools>=77 直接中止构建：

    InvalidConfigError: License classifiers have been superseded by license
    expressions (see PEP 639). Please remove: License :: OSI Approved :: MIT License

这对当时还带 classifier 的 funget / funimage / funread / funtts / nltcache
是「发版即失败」。
"""

import tomlkit

from funbuild.core.uv_build import UVBuild


def _apply(tmp_path, toml_text, license_file=True):
    if license_file:
        (tmp_path / "LICENSE").write_text("MIT License\n")
    config = tomlkit.parse(toml_text)
    builder = UVBuild.__new__(UVBuild)  # 跳过 __init__ 里的 git 探测
    builder._apply_license_metadata(config, str(tmp_path))
    return config


def test_license_classifier_removed(tmp_path):
    config = _apply(
        tmp_path,
        """
[project]
name = "demo"
classifiers = [
    "Programming Language :: Python :: 3",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
]
""",
    )
    cls = list(config["project"]["classifiers"])
    assert not any(c.startswith("License ::") for c in cls), cls
    # 其余 classifier 必须原样保留
    assert "Programming Language :: Python :: 3" in cls
    assert "Operating System :: OS Independent" in cls
    assert config["project"]["license"] == "MIT"


def test_wrong_license_classifier_also_removed(tmp_path):
    """funget / funread 曾是 Apache classifier 配 MIT LICENSE 文件，同样要清掉。"""
    config = _apply(
        tmp_path,
        """
[project]
name = "demo"
classifiers = [ "License :: OSI Approved :: Apache Software License",]
""",
    )
    assert list(config["project"]["classifiers"]) == []
    assert config["project"]["license"] == "MIT"


def test_legacy_table_license_converted(tmp_path):
    """`license = {file = "LICENSE"}` 这种旧表写法要转成 SPDX 字符串。"""
    config = _apply(
        tmp_path,
        """
[project]
name = "demo"
license = { text = "MIT" }
""",
    )
    assert config["project"]["license"] == "MIT"


def test_tool_setuptools_license_files_dropped(tmp_path):
    """`[tool.setuptools] license-files = []` 会让 wheel 不含协议文本，要清掉。"""
    config = _apply(
        tmp_path,
        """
[project]
name = "demo"

[tool.setuptools]
license-files = []
packages = ["demo"]
""",
    )
    assert "license-files" not in config["tool"]["setuptools"]
    assert list(config["project"]["license-files"]) == ["LICENSE"]
    # 同一张表里的其他键不能被误删
    assert list(config["tool"]["setuptools"]["packages"]) == ["demo"]


def test_no_license_file_means_no_declaration(tmp_path):
    """声明了 license-files 却找不到文件，setuptools 同样会构建失败。"""
    config = _apply(
        tmp_path,
        """
[project]
name = "demo"
license-files = ["LICENSE"]
""",
        license_file=False,
    )
    assert "license-files" not in config["project"]


def test_no_classifiers_key_is_fine(tmp_path):
    config = _apply(tmp_path, '[project]\nname = "demo"\n')
    assert config["project"]["license"] == "MIT"
