#!/usr/bin/env python3
"""把 Rust 版 funbuild 和改造前的 Python 版放在同一批仓库上跑 `upgrade`，逐字节比对结果。

`upgrade` 是最干净的差分面：只改清单文件，不碰网络、不碰 git 历史。JSON 缩进、
TOML 注释保留、pubspec 的 `+buildNumber`、组织元数据的表数组形状这些最容易出偏差
的地方都能在这里一次性抓出来。

用法：

    cargo build
    python3 scripts/differential.py                      # 基线取 PATH 上的 funbuild
    python3 scripts/differential.py --baseline /path/to/funbuild
    python3 scripts/differential.py --keep               # 保留工作目录便于排查
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / ".differential"

PYPROJECT_WITH_COMMENTS = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "funthing"
version = "1.6.54"
# 这条注释必须活过 upgrade
description = "demo"
requires-python = ">=3.12"
dependencies = [
    # 行内注释也要留住
    "requests>=2.0",
]

[tool.ruff]
line-length = 120

[tool.hatch.build.targets.wheel]
packages = ["src/funthing"]
"""

PACKAGE_JSON = '{\n  "name": "demo-web",\n  "version": "1.0.0",\n  "description": "中文说明",\n  "scripts": {\n    "build": "vite build"\n  }\n}\n'

FLUTTER_PUBSPEC = """\
name: demo_app
version: 1.0.0+7  # 商店构建号
environment:
  sdk: ">=3.0.0 <4.0.0"
dependencies:
  flutter:
    sdk: flutter
  funtrack: ^1.0.0
"""

MATURIN_PYPROJECT = """\
[build-system]
requires = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "fundemo"
version = "1.2.0"

[tool.maturin]
manifest-path = "crates/fundemo-py/Cargo.toml"
python-source = "src"
"""

# 每个 fixture 是「仓库目录名 -> {相对路径: 内容}」。目录名决定 is_org_repo 的
# 前缀判断（fixture 没有 remote），所以组织/非组织两条路径都要覆盖。
FIXTURES: dict[str, dict[str, str]] = {
    "funthing-uv": {
        "pyproject.toml": PYPROJECT_WITH_COMMENTS,
        "LICENSE": "MIT\n",
    },
    "somebody-uv": {
        # 非组织仓库：不该被塞进 authors / urls / license
        "pyproject.toml": PYPROJECT_WITH_COMMENTS.replace('name = "funthing"', 'name = "somebody"'),
    },
    "funthing-uv-classifiers": {
        # License classifier 必须被 PEP 639 取代，旧的 setuptools 键要清掉
        "pyproject.toml": """\
[project]
name = "funthing"
version = "1.0.0"
license = { text = "Apache-2.0" }
classifiers = [
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
]

[tool.setuptools]
license-files = []
""",
        "LICENSE.md": "Apache\n",
    },
    "funthing-uv-multi": {
        "pyproject.toml": PYPROJECT_WITH_COMMENTS,
        "extbuild/one/pyproject.toml": '[project]\nname = "one"\nversion = "0.0.9"\n',
        "exts/two/pyproject.toml": '[project]\nname = "two"\nversion = "0.0.9"\n',
        "exts/two/package.json": '{\n  "version": "0.0.9"\n}\n',
    },
    "funthing-uv-virtual": {
        "pyproject.toml": '[project]\nname = "funthing"\nversion = "1.0.0"\n\n[tool.uv]\npackage = false\n',
    },
    "fundemo-maturin": {
        "pyproject.toml": MATURIN_PYPROJECT,
        "Cargo.toml": '[workspace]\nmembers = ["crates/fundemo-py"]\n\n# 版本由 funbuild 同步写入\n[workspace.package]\nversion = "1.2.0"\nedition = "2021"\n',
        "crates/fundemo-py/Cargo.toml": '[package]\nname = "fundemo-py"\nversion.workspace = true\n',
        "crates/fundemo-core/Cargo.toml": '[package]\nname = "fundemo-core"\n# 独立的 0.x 线，不该被包版本冲掉\nversion = "0.2.0"\n',
    },
    "fundemo-cargo": {
        "Cargo.toml": '[package]\nname = "fundemo"\n# 这段注释必须留住\nversion = "0.3.11"\nedition = "2021"\n\n[dependencies]\nserde = { version = "1", features = ["derive"] }\n',
    },
    "funweb-npm": {
        "package.json": PACKAGE_JSON,
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    },
    "funapp-flutter": {
        "pubspec.yaml": FLUTTER_PUBSPEC,
    },
    "funscript-version": {
        "VERSION": "1.2.3\n",
    },
    "funold-poetry": {
        "pyproject.toml": '[tool.poetry]\nname = "funold"\n# 注释\nversion = "1.0.0"\n',
    },
    "funlegacy-pypi": {
        "script/__version__.md": "1.2.3",
    },
    "funproduct-dev": {
        "scripts/funbuild.toml": 'version = "2.0.91"\npackages = ["funflix"]\n',
        "apps/.keep": "",
    },
    "funhybrid": {
        "pyproject.toml": PYPROJECT_WITH_COMMENTS,
        "package.json": PACKAGE_JSON,
        "LICENSE": "MIT\n",
    },
    "funempty": {
        "README.md": "nothing to version here\n",
    },
}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        # 不打出被捕获的输出，失败原因就只剩一个退出码
        print(f"  ! {' '.join(command)} exit={result.returncode}")
        for line in (result.stdout or "").splitlines()[-5:]:
            print(f"    out: {line}")
        for line in (result.stderr or "").splitlines()[-5:]:
            print(f"    err: {line}")
    return result


def materialise(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    run(["git", "init", "--quiet"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    run(["git", "config", "user.name", "test"], cwd=repo)
    return repo


def tree_files(repo: Path) -> list[str]:
    """参与比对的文件。

    排除 `logs/`：Python 版经 farlog 会在它操作的每个仓库里落一份 funbuild.log，
    Rust 版只打 stderr。这是有意的行为变化，不是漂移。
    """
    return sorted(
        str(path.relative_to(repo))
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts and "logs" not in path.parts
    )


def unified(rel: str, a: str, b: str) -> str:
    import difflib

    return "\n".join(
        difflib.unified_diff(
            a.splitlines(),
            b.splitlines(),
            fromfile=f"baseline/{rel}",
            tofile=f"candidate/{rel}",
            lineterm="",
        )
    )


def compare(baseline: Path, candidate: Path) -> tuple[list[str], list[str]]:
    """比对两棵树，返回 (语义差异, 仅排版差异)。

    TOML 比的是解析后的数据而不是字节：Rust 版用 toml_edit，排版比 tomlkit 更
    保守——不会把用户写成多行的数组压成一行，也不会吃掉表之间的空行。那些是有意
    的改进，不该让差分一直红着；真正要守的是「写进去的值一样」。
    """
    fatal: list[str] = []
    cosmetic: list[str] = []

    left, right = tree_files(baseline), tree_files(candidate)
    if left != right:
        fatal.append(f"文件集合不同:\n    baseline={left}\n    candidate={right}")
        return fatal, cosmetic

    for rel in left:
        if filecmp.cmp(baseline / rel, candidate / rel, shallow=False):
            continue
        a = (baseline / rel).read_text(encoding="utf-8", errors="replace")
        b = (candidate / rel).read_text(encoding="utf-8", errors="replace")
        diff = unified(rel, a, b)
        if rel.endswith(".toml"):
            import tomllib

            try:
                if tomllib.loads(a) == tomllib.loads(b):
                    cosmetic.append(f"{rel} 排版不同但数据一致:\n{diff}")
                    continue
            except tomllib.TOMLDecodeError as error:
                fatal.append(f"{rel} 解析失败: {error}")
                continue
        fatal.append(f"{rel} 内容不同:\n{diff}")
    return fatal, cosmetic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="funbuild", help="改造前的 Python 版 funbuild 可执行文件")
    parser.add_argument(
        "--candidate",
        default=None,
        help="新的 Rust 二进制，默认取 cargo 的 debug 产物",
    )
    parser.add_argument("--keep", action="store_true", help="保留 .differential/ 工作目录")
    parser.add_argument("--command", default="upgrade", help="要比对的子命令，默认 upgrade")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印仅排版不同的细节")
    args = parser.parse_args()

    baseline = shutil.which(args.baseline) or args.baseline
    if not Path(baseline).exists():
        print(f"找不到基线可执行文件: {args.baseline}")
        return 1

    if args.candidate:
        candidate = Path(args.candidate).resolve()
    else:
        probe = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        import json

        target = json.loads(probe.stdout)["target_directory"] if probe.returncode == 0 else str(REPO_ROOT / "target")
        candidate = Path(target) / "debug" / "funbuild"
    if not candidate.exists():
        print(f"找不到 Rust 二进制 {candidate}，先跑 `cargo build`")
        return 1

    print(f"baseline : {baseline}")
    print(f"candidate: {candidate}\n")

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    (WORK_DIR / "baseline").mkdir(parents=True)
    (WORK_DIR / "candidate").mkdir(parents=True)

    failures = 0
    cosmetic_only = 0
    for name, files in FIXTURES.items():
        left = materialise(WORK_DIR / "baseline", name, files)
        right = materialise(WORK_DIR / "candidate", name, files)
        run([baseline, args.command], cwd=left)
        run([str(candidate), args.command], cwd=right)

        fatal, cosmetic = compare(left, right)
        if fatal:
            failures += 1
            print(f"✖ {name}")
            for problem in fatal:
                print(f"  {problem}")
        elif cosmetic:
            cosmetic_only += 1
            print(f"✔ {name} (排版有差异，数据一致)")
            if args.verbose:
                for note in cosmetic:
                    print(f"  {note}")
        else:
            print(f"✔ {name}")

    print()
    total = len(FIXTURES)
    if failures:
        print(f"{total - failures}/{total} 数据一致，{failures} 个有语义差异")
    else:
        suffix = f"（其中 {cosmetic_only} 个仅排版不同，加 -v 看详情）" if cosmetic_only else ""
        print(f"{total}/{total} 全部一致{suffix}")
    if not args.keep:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
