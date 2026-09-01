#!/usr/bin/python3

import os
import shlex

import yaml

from .base import BaseBuild
from .util import logger
from .version_sync import replace_pubspec_version_line, sync_all_manifest_versions

# funpub (https://pypi.org/project/funpub/) 上传到的私有通用仓库名; 由使用方
# 通过 `funsecret write ... funpub aliyun generic funpackage ...` 预先配置好
# repo-url/username/password, 这里只需要 --repo-name 对上。
FUNPUB_REPO_NAME = "funpackage"


class FlutterBuild(BaseBuild):
    """基于 pubspec.yaml 的 Flutter 项目构建。

    版本号沿用 pubspec.yaml 的 `major.minor.patch+buildNumber` 惯例:
    major.minor.patch 走与其它构建类型相同的 128 进制自增, buildNumber 每次
    upgrade 单独 +1 (应用商店要求其严格递增, 不能随主版本号一起被基类的
    parse_version 当成后缀丢弃)。
    """

    PUBSPEC_PATH = "./pubspec.yaml"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_number: int | None = None
        self._funbuild_cfg: dict = {}
        self._pubspec_name: str = self.name

    def check_type(self) -> bool:
        if not os.path.isfile(self.PUBSPEC_PATH):
            return False
        try:
            with open(self.PUBSPEC_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"skip pubspec.yaml, cannot parse: {e}")
            return False
        if not isinstance(data, dict):
            return False

        # 必须显式依赖 Flutter SDK, 用来排除纯 Dart (无 flutter 依赖) 的包 ——
        # 那类项目应该走 `dart pub` 工具链, 不在本构建类型的范围内。
        deps = data.get("dependencies")
        flutter_dep = isinstance(deps, dict) and deps.get("flutter")
        uses_flutter_sdk = isinstance(flutter_dep, dict) and flutter_dep.get("sdk") == "flutter"
        if not uses_flutter_sdk:
            return False

        raw_version = data.get("version")
        if not isinstance(raw_version, str) or not raw_version.strip():
            return False
        main, _, build = raw_version.strip().partition("+")
        self.version = main.strip()
        self._build_number = int(build) if build.isdigit() else None

        cfg = data.get("funbuild")
        self._funbuild_cfg = cfg if isinstance(cfg, dict) else {}

        pubspec_name = data.get("name")
        if isinstance(pubspec_name, str) and pubspec_name.strip():
            self._pubspec_name = pubspec_name.strip()
        return True

    def _write_version(self):
        with open(self.PUBSPEC_PATH, encoding="utf-8") as f:
            raw = f.read()
        next_build = self._build_number + 1 if self._build_number is not None else None
        new_token = self.version if next_build is None else f"{self.version}+{next_build}"
        new_raw, count = replace_pubspec_version_line(raw, lambda _inner: new_token)
        if count == 0:
            logger.warning(f"{self.PUBSPEC_PATH} 未找到 version 字段, 跳过写入")
            return
        with open(self.PUBSPEC_PATH, "w", encoding="utf-8") as f:
            f.write(new_raw)
        self._build_number = next_build
        sync_all_manifest_versions(self.version)

    @staticmethod
    def _as_command_list(value) -> list[str] | None:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list):
            cmds = [v.strip() for v in value if isinstance(v, str) and v.strip()]
            return cmds or None
        return None

    def _cmd_build(self) -> list[str]:
        custom = self._as_command_list(self._funbuild_cfg.get("build"))
        if custom is not None:
            return ["flutter pub get", *custom]
        return [
            "flutter pub get",
            "flutter build apk --release",
            "flutter build web --release",
        ]

    def _cmd_install(self) -> list[str]:
        custom = self._as_command_list(self._funbuild_cfg.get("install"))
        return custom or []

    # 默认构建产物的落盘位置, 与 _cmd_build 的默认 `flutter build apk/web` 对应。
    _APK_PATH = "build/app/outputs/flutter-apk/app-release.apk"
    _WEB_DIR = "build/web"
    _WEB_ZIP = "build/web-release.zip"

    def _cmd_publish(self) -> list[str]:
        """默认经 funpub 上传到私有通用仓库 (repo-name 固定为 funpackage,
        账号/地址由 funsecret 预先配置好, 这里不经手凭据)。

        apk 是单文件直接传; web 产物是目录, 先在子 shell 里打包成 zip 再传,
        避免 zip 打包时的 cd 通过 `&&` 串联泄漏到后续命令。在 pubspec.yaml 里
        配置 `funbuild.publish` (字符串或字符串数组) 可完全接管这一步, 例如
        换成自定义的应用商店发布脚本。
        """
        custom = self._as_command_list(self._funbuild_cfg.get("publish"))
        if custom is not None:
            return custom
        version = shlex.quote(self.version)
        zip_from_web_dir = os.path.join("..", os.path.basename(self._WEB_ZIP))
        return [
            f"(cd {shlex.quote(self._WEB_DIR)} && zip -rq {shlex.quote(zip_from_web_dir)} .)",
            f"funpub upload {shlex.quote(self._APK_PATH)} flutter/{self._pubspec_name}/apk "
            f"--version {version} --repo-name {FUNPUB_REPO_NAME}",
            f"funpub upload {shlex.quote(self._WEB_ZIP)} flutter/{self._pubspec_name}/web "
            f"--version {version} --repo-name {FUNPUB_REPO_NAME}",
        ]

    def _cmd_delete(self) -> list[str]:
        custom = self._funbuild_cfg.get("cleanDirs")
        if isinstance(custom, list) and custom:
            return [f"rm -rf {d}" for d in custom if isinstance(d, str) and d.strip()]
        return ["flutter clean"]
