#!/usr/bin/python3


import json
import os
import shlex
import traceback
import typing
from configparser import ConfigParser
from typing import Union

import toml
import typer
from funshell import run_shell, run_shell_list
from nltlog import getLogger

logger = getLogger("nltbuild")


def opencommit_commit(default_message: str = "add") -> bool:
    """使用 opencommit CLI 自动提交, 成功返回 True。"""
    try:
        diff: str = run_shell("git diff --staged", printf=False)
        if not diff:
            logger.warning("No staged changes")
            return False

        run_shell("aicommits --yes")
        return True
    except Exception as e:
        traceback.print_exc()
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


class BaseBuild:
    """构建工具的基类"""

    def __init__(self, name=None):
        # 获取git仓库根目录路径
        self.repo_path = run_shell("git rev-parse --show-toplevel", printf=False)
        # 设置项目名称,默认为仓库名
        self.name = name or self.repo_path.split("/")[-1]
        self.version = None

    def check_type(self) -> bool:
        """检查是否为当前构建类型"""
        raise NotImplementedError

    def _write_version(self):
        """写入版本号"""
        raise NotImplementedError

    def __version_upgrade(self, step=128):
        """版本号自增"""
        version = self.version
        if version is None:
            version = "0.0.1"

        version1 = [int(i) for i in version.split(".")]
        version2 = version1[0] * step * step + version1[1] * step + version1[2] + 1

        version1[2] = version2 % step
        version1[1] = int(version2 / step) % step
        version1[0] = int(version2 / step / step)

        return "{}.{}.{}".format(*version1)

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
        run_shell_list(["git pull"])

    def push(self, message="add", *args, **kwargs):
        """推送代码"""
        logger.info(f"{self.name} push")
        run_shell_list(["git add -A"])
        run_shell_list(["aicommits --yes"])
        run_shell_list(["git push"])
        # run_shell_list([f'git commit -a -m "{message}"', "git push"])

    def install(self, *args, **kwargs):
        """安装包"""
        logger.info(f"{self.name} install")
        run_shell_list(self._cmd_build() + self._cmd_install() + self._cmd_delete())

    def build(self, message="add", *args, **kwargs):
        """构建发布流程"""
        logger.info(f"{self.name} build")
        self.pull()
        self.upgrade()
        run_shell_list(
            self._cmd_delete() + self._cmd_build() + self._cmd_install() + self._cmd_publish() + self._cmd_delete()
        )
        self.push(message=message)
        self.tags()

    def clean_history(self, *args, **kwargs):
        """清理git历史记录"""
        logger.info(f"{self.name} clean history")
        run_shell_list(
            [
                "git tag -d $(git tag -l) || true",  # 删除本地 tag
                "git fetch",  # 拉取远程tag
                "git push origin --delete $(git tag -l)",  # 删除远程tag
                "git tag -d $(git tag -l) || true",  # 删除本地tag
                "git checkout --orphan latest_branch",  # 1.Checkout
                "git add -A",  # 2.Add all the files
                'git commit -am "clear history"',  # 3.Commit the changes
                "git branch -D master",  # 4.Delete the branch
                "git branch -m master",  # 5.Rename the current branch to master
                "git push -f origin master",  # 6.Finally, force update your repository
                "git push --set-upstream origin master",
                f"echo {self.name} success",
            ]
        )

    def clean(self, *args, **kwargs):
        """清理git缓存"""
        logger.info(f"{self.name} clean")
        run_shell_list(
            [
                "git rm -r --cached .",
                "git add .",
                "git commit -m 'update .gitignore'",
                "git gc --aggressive",
            ]
        )

    def tags(self, *args, **kwargs):
        """创建版本标签"""
        run_shell_list(
            [
                f"git tag v{self.version}",
                "git push --tags",
            ]
        )


class PypiBuild(BaseBuild):
    """PyPI构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version_path = "./script/__version__.md"

    def check_type(self):
        """检查是否为PyPI项目"""
        if os.path.exists(self.version_path):
            self.version = open(self.version_path, "r").read().strip()  # noqa: UP015
            return True
        return False

    def _write_version(self):
        """写入版本号到文件"""
        with open(self.version_path, "w") as f:
            f.write(self.version)
        sync_all_manifest_versions(self.version)

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return []

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return [
            "pip install dist/*.whl",
        ]


class PoetryBuild(BaseBuild):
    """Poetry构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_path = "./pyproject.toml"

    def check_type(self) -> bool:
        """检查是否为Poetry项目"""
        if os.path.exists(self.toml_path):
            a = toml.load(self.toml_path)
            if "tool" in a:
                self.version = a["tool"]["poetry"]["version"]
                return True
        return False

    def _write_version(self):
        """写入版本号到pyproject.toml"""
        a = toml.load(self.toml_path)
        a["tool"]["poetry"]["version"] = self.version
        with open(self.toml_path, "w") as f:
            toml.dump(a, f)
        sync_all_manifest_versions(self.version)

    def _cmd_publish(self) -> list[str]:
        """发布命令"""
        return ["poetry publish"]

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return ["poetry lock", "poetry build"]


class UVBuild(BaseBuild):
    """UV构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toml_paths = ["./pyproject.toml"]

        # 扫描extbuild和exts目录下的pyproject.toml
        for root in ("extbuild", "exts"):
            if os.path.isdir(root):
                for file in os.listdir(root):
                    path = os.path.join(root, file)
                    if os.path.isdir(path):
                        toml_path = os.path.join(path, "pyproject.toml")
                        if os.path.exists(toml_path):
                            self.toml_paths.append(toml_path)

    def check_type(self) -> bool:
        """检查是否为UV项目"""
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            if "project" in a:
                self.version = a["project"]["version"]
                return True
        return False

    def _write_version(self):
        """写入版本号到所有pyproject.toml"""
        for toml_path in self.toml_paths:
            try:
                config = toml.load(toml_path)
                self.config_format(config)
                config["project"]["version"] = self.version
                with open(toml_path, "w") as f:
                    toml.dump(config, f)
            except Exception as e:
                logger.error(f"Failed to update version in {toml_path}: {e}")
                raise  # 重要错误应该向上传播
        sync_all_manifest_versions(self.version)

    def config_format(self, config):
        """格式化配置文件"""
        if not self.name.startswith("fun"):
            return
        deep_create(config, "tool", "setuptools", key="license-files", value=[])
        deep_create(
            config,
            "project",
            key="authors",
            value=[
                {"name": "牛哥", "email": "niuliangtao@qq.com"},
                {"name": "farfarfun", "email": "farfarfun@qq.com"},
            ],
        )
        deep_create(
            config,
            "project",
            key="maintainers",
            value=[
                {"name": "牛哥", "email": "niuliangtao@qq.com"},
                {"name": "farfarfun", "email": "farfarfun@qq.com"},
            ],
        )
        deep_create(
            config,
            "project",
            key="urls",
            value={
                "Organization": "https://github.com/farfarfun",
                "Repository": f"https://github.com/farfarfun/{self.name}",
                "Releases": f"https://github.com/farfarfun/{self.name}/releases",
            },
        )
        if "Add your description here" in config["project"]["description"]:
            deep_create(config, "project", key="description", value=f"{self.name}")

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return [
            *super()._cmd_delete(),
            "rm -rf src/*.egg-info",
            "rm -rf extbuild/*/src/*.egg-info",
            "rm -rf exts/*/src/*.egg-info",
        ]

    def _cmd_publish(self) -> list[str]:
        """发布命令"""
        config = ConfigParser()

        config.read(f"{os.environ['HOME']}/.pypirc")

        server = config["distutils"]["index-servers"].strip().split()[0]
        if os.path.exists(self.toml_paths[0]):
            a = toml.load(self.toml_paths[0])
            server = deep_get(a, "tool", "uv", "index", 0, "name") or server
        logger.info(f"public server: {server}")
        settings = config[server]
        opts = []
        if user := settings.get("username"):
            password = settings.get("password")

            if "__token__" in user:
                if password:
                    opts.append(f"--token={password}")
            else:
                opts.append(f"--username={user}")
                if password:
                    opts.append(f"--password='{password}'")

            url = settings.get("repository")
            if url and opts:
                opts.append(f"--publish-url={url}")
        a = ["uv", "publish", *opts]
        return [" ".join(a)]

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        result = [
            # "uv sync --prerelease=allow -U",
            "uv lock --prerelease=allow"
        ]
        if self.name.startswith("fun"):
            # result.append("uvx ruff clean")
            result.append("uvx ruff format")
            result.append("uvx ruff clean")
        for toml_path in self.toml_paths:
            result.append(f"uv build -q --wheel --prerelease=allow --directory {os.path.dirname(toml_path)}")
        return result

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return ["uv pip install dist/*.whl"]


class NpmFrontendBuild(BaseBuild):
    """基于 package.json 的前端项目构建与发布 (npm / pnpm / yarn), 支持 extbuild 子目录多包。"""

    ROOT_PACKAGE_JSON = "./package.json"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.package_json_paths: list[str] = []
        self._pkg: dict = {}
        self._pm = "npm"
        self._nltbuild_cfg: dict = {}

    @staticmethod
    def _load_json_at(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _nltbuild_from_pkg(pkg: dict) -> dict:
        raw = pkg.get("nltbuild")
        if isinstance(raw, dict):
            return raw
        if raw is True:
            return {"enabled": True}
        return {}

    @staticmethod
    def _package_qualifies(pkg: dict) -> bool:
        ver = pkg.get("version")
        if not isinstance(ver, str) or not ver.strip():
            return False
        cfg = NpmFrontendBuild._nltbuild_from_pkg(pkg)
        scripts = pkg.get("scripts") or {}
        custom_build = isinstance(cfg.get("build"), str) and bool(cfg["build"].strip())
        has_build = isinstance(scripts, dict) and "build" in scripts
        return bool(has_build or custom_build)

    def _collect_package_json_paths(self) -> list[str]:
        paths: list[str] = []
        if os.path.isfile(self.ROOT_PACKAGE_JSON):
            try:
                pkg = self._load_json_at(self.ROOT_PACKAGE_JSON)
                if self._package_qualifies(pkg):
                    paths.append(self.ROOT_PACKAGE_JSON)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"skip root package.json: {e}")
        ext_root = "extbuild"
        if os.path.isdir(ext_root):
            for name in sorted(os.listdir(ext_root)):
                sub = os.path.join(ext_root, name)
                if not os.path.isdir(sub):
                    continue
                pj = os.path.join(sub, "package.json")
                if not os.path.isfile(pj):
                    continue
                try:
                    pkg = self._load_json_at(pj)
                    if self._package_qualifies(pkg):
                        paths.append(pj)
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(f"skip {pj}: {e}")
        return paths

    def _detect_package_manager_for_dir(self, pkg_dir: str, cfg: dict) -> str:
        """推断包管理器: 显式配置 > pnpm > npm > yarn。"""
        pm = cfg.get("packageManager")
        if pm in ("npm", "pnpm", "yarn"):
            return pm
        if os.path.exists(os.path.join(pkg_dir, "pnpm-lock.yaml")):
            return "pnpm"
        if os.path.exists(os.path.join(pkg_dir, "package-lock.json")):
            return "npm"
        if os.path.exists(os.path.join(pkg_dir, "yarn.lock")):
            return "yarn"
        return "npm"

    @staticmethod
    def _in_dir_shell(pkg_dir: str, inner: str) -> str:
        d = os.path.normpath(pkg_dir)
        if d in (".", ""):
            return inner
        return f"cd {shlex.quote(d)} && {inner}"

    def _sync_primary_state(self):
        primary = self.package_json_paths[0]
        self._pkg = self._load_json_at(primary)
        self._nltbuild_cfg = self._nltbuild_from_pkg(self._pkg)
        ver = self._pkg.get("version")
        if isinstance(ver, str) and ver.strip():
            self.version = ver.strip()
        pkg_dir = os.path.dirname(primary)
        self._pm = self._detect_package_manager_for_dir(pkg_dir, self._nltbuild_cfg)

    def check_type(self) -> bool:
        self.package_json_paths = self._collect_package_json_paths()
        if not self.package_json_paths:
            return False
        self._sync_primary_state()
        return True

    def _write_version(self):
        for pj in self.package_json_paths:
            pkg = self._load_json_at(pj)
            pkg["version"] = self.version
            with open(pj, "w", encoding="utf-8") as f:
                json.dump(pkg, f, indent=2, ensure_ascii=False)
                f.write("\n")
        sync_all_manifest_versions(self.version)
        self._sync_primary_state()

    def _install_cmds_for_dir(self, pkg_dir: str, cfg: dict, pm: str) -> list[str]:
        custom = cfg.get("install")
        if isinstance(custom, str) and custom.strip():
            return [custom.strip()]
        if pm == "pnpm":
            lock = os.path.join(pkg_dir, "pnpm-lock.yaml")
            return ["pnpm install --frozen-lockfile" if os.path.exists(lock) else "pnpm install"]
        if pm == "yarn":
            lock = os.path.join(pkg_dir, "yarn.lock")
            return ["yarn install --frozen-lockfile" if os.path.exists(lock) else "yarn install"]
        lock = os.path.join(pkg_dir, "package-lock.json")
        return ["npm ci" if os.path.exists(lock) else "npm install"]

    def _build_cmd_for(self, cfg: dict, pm: str) -> str:
        custom = cfg.get("build")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        if pm == "yarn":
            return "yarn run build"
        return f"{pm} run build"

    def _cmd_build(self) -> list[str]:
        out: list[str] = []
        for pj in self.package_json_paths:
            pkg_dir = os.path.dirname(pj)
            pkg = self._load_json_at(pj)
            cfg = self._nltbuild_from_pkg(pkg)
            pm = self._detect_package_manager_for_dir(pkg_dir, cfg)
            for cmd in self._install_cmds_for_dir(pkg_dir, cfg, pm):
                out.append(self._in_dir_shell(pkg_dir, cmd))
            out.append(self._in_dir_shell(pkg_dir, self._build_cmd_for(cfg, pm)))
        return out

    def install(self, *args, **kwargs):
        logger.info(f"{self.name} install (frontend dependencies)")
        if not self.package_json_paths:
            self.package_json_paths = self._collect_package_json_paths()
        if not self.package_json_paths:
            return
        cmds: list[str] = []
        for pj in self.package_json_paths:
            pkg_dir = os.path.dirname(pj)
            pkg = self._load_json_at(pj)
            cfg = self._nltbuild_from_pkg(pkg)
            pm = self._detect_package_manager_for_dir(pkg_dir, cfg)
            for c in self._install_cmds_for_dir(pkg_dir, cfg, pm):
                cmds.append(self._in_dir_shell(pkg_dir, c))
        run_shell_list(cmds)

    def _cmd_install(self) -> list[str]:
        return []

    def _publish_cmds_for_package(self, pj: str) -> list[str]:
        pkg = self._load_json_at(pj)
        cfg = self._nltbuild_from_pkg(pkg)
        pkg_dir = os.path.dirname(pj)
        pm = self._detect_package_manager_for_dir(pkg_dir, cfg)
        if cfg.get("publish") is False:
            return []
        custom = cfg.get("publish")
        if isinstance(custom, str) and custom.strip():
            return [self._in_dir_shell(pkg_dir, custom.strip())]
        if pkg.get("private") is True:
            logger.info(f"{pj}: private=true, skip publish; set nltbuild.publish to override")
            return []
        if pm == "pnpm":
            pub = "pnpm publish --no-git-checks"
        elif pm == "yarn":
            pub = "yarn npm publish"
        else:
            pub = "npm publish"
        return [self._in_dir_shell(pkg_dir, pub)]

    def _cmd_publish(self) -> list[str]:
        out: list[str] = []
        for pj in self.package_json_paths:
            out.extend(self._publish_cmds_for_package(pj))
        return out

    def _cmd_delete(self) -> list[str]:
        dirs = self._nltbuild_cfg.get("cleanDirs")
        if isinstance(dirs, list) and dirs:
            return [f"rm -rf {d}" for d in dirs if isinstance(d, str) and d.strip()]
        return [
            "rm -rf dist",
            "rm -rf build",
            "rm -rf .next",
            "rm -rf out",
            "rm -rf storybook-static",
            "rm -rf extbuild/*/dist",
            "rm -rf extbuild/*/build",
            "rm -rf extbuild/*/.next",
            "rm -rf extbuild/*/out",
            "rm -rf extbuild/*/storybook-static",
        ]


class EmptyBuild(BaseBuild):
    """UV构建类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def check_type(self) -> bool:
        """检查是否为UV项目"""
        return True

    def _write_version(self):
        pass

    def config_format(self, config):
        pass

    def _cmd_delete(self) -> list[str]:
        """清理命令"""
        return []

    def _cmd_publish(self) -> list[str]:
        return []

    def _cmd_build(self) -> list[str]:
        """构建命令"""
        return []

    def _cmd_install(self) -> list[str]:
        """安装命令"""
        return []


def get_build() -> Union[UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]:
    """获取合适的构建类"""
    builders = [UVBuild, PoetryBuild, PypiBuild, NpmFrontendBuild, EmptyBuild]
    for builder in builders:
        build = builder()
        if build.check_type():
            return build

    raise Exception("未找到合适的构建类")


def nltbuild():
    """主入口函数"""
    builder = get_build()
    cli = typer.Typer(help='build tool for "fun"')

    @cli.command()
    def upgrade():
        """升级版本"""
        builder.upgrade()

    @cli.command()
    def pull():
        """拉取代码"""
        builder.pull()

    @cli.command()
    # @click.option("--message", type=str, default="add", help="commit message")
    # @click.option("--name", type=str, default="fun", help="build name")
    def push(
        message: str = "add",
    ):
        """推送代码"""
        builder.push(
            message,
        )

    @cli.command()
    def install():
        """安装包"""
        builder.install()

    @cli.command()
    def build(
        message: typing.Annotated[str, typer.Argument(help="提交时的commit信息")] = "add",
    ):
        """构建发布"""
        builder.build(message=message)

    @cli.command()
    def clean_history():
        """清理历史"""
        builder.clean_history()

    @cli.command()
    def clean():
        """清理缓存"""
        builder.clean()

    @cli.command()
    def tags():
        """创建标签"""
        builder.tags()

    cli()
