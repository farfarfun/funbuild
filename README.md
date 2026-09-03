# funbuild

[![PyPI version](https://badge.fury.io/py/funbuild.svg)](https://badge.fury.io/py/funbuild)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**funbuild** 是面向 Python / 混合仓库的构建与发布辅助工具：根据项目结构自动选择构建策略（UV、Poetry、旧式 PyPI 脚本、`package.json` 前端包、Flutter 及混合模式等），串联版本递增、构建、安装校验、发布与 Git 标签等常见流程。

## 特性

- **多构建策略**：按仓库布局自动匹配 `UVBuild`、`PoetryBuild`、`PypiBuild`、`NpmFrontendBuild`、`UvNpmHybridBuild` 等实现，无需手写切换逻辑。
- **版本同步**：以根目录 `pyproject.toml` 的 `[project].version` 为主源时，可将版本同步到仓内其它带 `version` 的 `pyproject.toml`、`package.json` 与 `pubspec.yaml`（含子目录；`pubspec.yaml` 只同步 `major.minor.patch`，`+buildNumber` 保持不变）。
- **依赖与工具链**：内置对 **uv**、**ruff** 等工具的调用约定；日志通过 **farlog**，Shell 流程通过 **funshell**。
- **Git 工作流**：`pull` / `push` / `tag` 等与远程协作；`push` 在提交阶段优先用 **aicommits** 生成说明，未安装时自动回退到默认信息。
- **失败即中止**：任一 shell 步骤返回非 0 即抛出 `ShellCommandError` 并以非 0 码退出，构建失败不会继续推送或打标签。
- **维护命令**：`clean` 与 `clean-history` 会改写 Git 状态或强制重写远程历史，使用前请确认团队规范与备份策略。

## 打包支持的项目类型

`funbuild` 运行时会按下表顺序（从上到下）依次探测仓库根目录，命中第一个匹配的类型即停止；判定逻辑见 `src/funbuild/core/registry.py`。

| 优先级 | 构建类型 | 判定条件 | 适用项目 |
| --- | --- | --- | --- |
| 1 | `UvNpmHybridBuild` | 同时满足 `UVBuild` 与 `NpmFrontendBuild` 的判定条件 | 同一仓库里既是 UV 管理的 Python 包、又带前端 `package.json` 的混合项目（一条命令串联两侧构建/安装/发布） |
| 2 | `UVBuild` | 根目录存在 `pyproject.toml` 且含 `[project]` 段 | 使用 `uv` / PEP 621 标准 `pyproject.toml` 的现代 Python 包（含 `extbuild/`、`exts/` 子包） |
| 3 | `PoetryBuild` | `pyproject.toml` 存在且 `[tool.poetry].version` 有效 | 使用 Poetry 管理版本与依赖的 Python 项目 |
| 4 | `PypiBuild` | 根目录存在 `script/__version__.md` | 早期遗留的 PyPI 发布脚本项目 |
| 5 | `FlutterBuild` | `pubspec.yaml` 存在且 `dependencies.flutter.sdk == flutter` | Flutter 应用/插件项目 |
| 6 | `NpmFrontendBuild` | 存在 `package.json`（含 `extbuild/` 等子目录的多包场景） | npm / pnpm / yarn 管理的纯前端项目 |
| 7 | `VersionFileBuild` | 根目录存在纯文本 `VERSION` 文件 | 没有 `pyproject.toml` / `package.json`、无需真正构建的仓库（如 Shell 脚本仓库），仅借用 `upgrade` / `push` / `tag` 的版本与 Git 流程 |
| 8 | `EmptyBuild` | 兜底，永远匹配 | 未识别到任何版本清单的仓库；`upgrade`、`build`、`tag` 均为空操作，仅保留提示日志 |

### Flutter 项目 (`FlutterBuild`)

版本号沿用 `pubspec.yaml` 的 `major.minor.patch+buildNumber` 惯例：`upgrade` 按 128 进制递增 `major.minor.patch`，同时把 `+buildNumber` 单独 `+1`（应用商店要求构建号严格递增，不与主版本号混在一起处理）。

默认命令：

| 阶段 | 默认命令 |
| --- | --- |
| 构建 | `flutter pub get` + `flutter build apk --release` + `flutter build web --release` |
| 安装 | 无（本地一般没有可安装的产物形态） |
| 发布 | 经 [`funpub`](https://pypi.org/project/funpub/) 上传到私有通用仓库（`--repo-name funpackage`）：apk 直传，web 产物先打包成 zip 再传，远端路径为 `flutter/{pubspec name}/apk` 与 `flutter/{pubspec name}/web` |
| 清理 | `flutter clean` |

可在 `pubspec.yaml` 中加一段 `funbuild:` 来覆盖任意阶段（字符串或字符串数组均可，数组会依次执行）：

```yaml
funbuild:
  build: flutter build ios --release        # 或写成数组: [flutter build apk, flutter build appbundle]
  install: flutter install
  publish: curl -T build/app/outputs/flutter-apk/app-release.apk -u "$ARTIFACT_USER:$ARTIFACT_PASS" https://packages.example.com/generic/demo/app-release.apk
  cleanDirs: [build, .dart_tool]
```

> 默认发布走 `funpub upload`，仓库固定为 `funpackage`（阿里云 Packages generic 仓库）；账号、地址等凭据不经 funbuild 之手，需提前用 `funsecret write ... funpub aliyun generic funpackage ...` 配置好，`funbuild` 只负责拼装 `funpub upload` 命令，不接触任何凭据。如果你的团队使用别的制品仓库（自建 Nexus 等）或需要不同的 `repo-name`，在 `funbuild.publish` 里写你自己的上传命令即可完全接管这一步，同样能接入 `funbuild build` 的完整流水线。

## 系统要求

- Python 3.10+
- Git（版本管理与标签推送）
- 可选：`aicommits`（`npm install -g aicommits`）。装了则 `push` 用它生成提交信息，没装则回退到 `message` 参数的值，不影响流程。
- Flutter 项目需要本机装好 `flutter` 命令并加入 `PATH`；`funbuild` 本身只负责拼装 `flutter` 命令并不校验其可用性。
- Flutter 项目的默认发布步骤需要安装 [`funpub`](https://pypi.org/project/funpub/)（`pip install funpub`），并提前用 `funsecret` 配置好 `funpackage` 仓库的凭据，否则 `funpub upload` 会失败。

## 安装

### 从 PyPI 安装

```bash
pip install funbuild
```

### 从源码安装

```bash
git clone https://github.com/farfarfun/funbuild.git
cd funbuild
pip install .
```

使用 [uv](https://github.com/astral-sh/uv) 时，可在克隆后执行 `uv sync` 或 `uv pip install -e .` 进行可编辑安装。

## 命令一览

在项目根目录执行（入口由 `pyproject.toml` 的 `[project.scripts]` 注册为 `funbuild`）：

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `upgrade` | — | 版本自增并写回各清单文件 |
| `pull` | — | `git pull` |
| `push` | `--message`（默认 `add`）<br>`--batch-size`（默认 `20`） | 按文件修改时间从旧到新分批提交，最后统一推送 |
| `install` | — | 构建 + 安装到当前环境 + 清理产物 |
| `build` | `message`（位置参数，默认 `add`） | 完整发布流水线，见下 |
| `release` | 同 `build` | `build` 的别名，行为完全一致 |
| `tag` | — | 打 `v{version}` 标签并推送 |
| `clean` | — | 重建 Git 索引以应用新的 `.gitignore`（会产生一次提交） |
| `clean-history` | — | **破坏性**：删除全部标签与提交历史并强推远程 |

> 命令名中的下划线会被 typer 转成连字符，因此是 `clean-history` 而非 `clean_history`。

### 版本

```bash
funbuild upgrade
```

自增规则为 **128 进制进位**：patch 满 128 向 minor 进位，minor 满 128 向 major 进位。

```
1.6.54  → 1.6.55
1.6.127 → 1.7.0
1.127.127 → 2.0.0
```

版本号会写回根 `pyproject.toml`，并同步到仓内所有带 `version` 字段的 `pyproject.toml`、`package.json` 与 `pubspec.yaml`（含 `extbuild/` `exts/` 子目录）。非三段版本按缺位补 0 处理（`1.0` 视作 `1.0.0`）；`1.0.0rc1` 这类预发布后缀会被丢弃并打印告警。以 `FlutterBuild` 为主构建类型时例外：`pubspec.yaml` 自己的 `+buildNumber` 每次 `upgrade` 单独 `+1`，不受该丢弃规则影响。

CLI 未提供「写入指定版本号」参数，需要固定版本时请直接编辑清单文件。

### Git

```bash
funbuild pull

# 默认每 20 个文件一次提交
funbuild push

# 自定义每个提交的文件数
funbuild push --batch-size 50

# 指定提交信息（未安装 aicommits 时生效）；注意这里是选项而非位置参数
funbuild push --message "fix: typo"
```

> `push` 会先执行 `git reset` 清空暂存区再按批次重新 `add`，已手工 `git add` 的内容会被一并纳入分批提交。

### 构建与发布

```bash
# 完整流水线
funbuild build

# release 是 build 的别名，两者完全等价
funbuild release

# 仅构建并安装到当前环境，不发布、不推送
funbuild install
```

`build` 依次触发：`pull` → `upgrade` → 清理 → 构建 → 安装校验 → 发布 → 清理 → `push` → `tag`。实际命令序列取决于选中的 Build 类型。任一步失败会立即中止，不会继续 push 或打标签。

### 维护类（高风险）

```bash
funbuild clean          # 重建索引，使新增的 .gitignore 规则生效
funbuild clean-history  # 抹掉全部历史与标签并强推，不可恢复且无二次确认
```

## 发布凭据

`uv publish` 的凭据优先读取 `UV_PUBLISH_TOKEN` / `UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD` / `UV_PUBLISH_URL` 环境变量；某个变量未设置时，才用 `~/.pypirc` 里对应的值补齐（已设置的环境变量不会被 `~/.pypirc` 覆盖）。服务器名的选取顺序为：`pyproject.toml` 中 `[[tool.uv.index]]` 的 `name` > `~/.pypirc` 里 `[distutils].index-servers` 的首项 > 默认 `pypi`。

```ini
[distutils]
index-servers = pypi

[pypi]
username = __token__
password = pypi-AgEIcHl...
```

凭据以 `UV_PUBLISH_TOKEN` / `UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD` / `UV_PUBLISH_URL` 环境变量传给 uv，**不会出现在命令行参数中**（否则完整命令行会通过进程表对本机所有用户可见）。

CI 环境可以不放 `.pypirc`，直接导出这些环境变量即可：

```bash
export UV_PUBLISH_TOKEN=pypi-AgEIcHl...
funbuild build
```

## 错误处理

所有 shell 步骤经 `run_checked()` 执行，退出码非 0 即抛 `ShellCommandError`，进程以非 0 码退出。这意味着 `funbuild build` 在构建或发布失败时不会再继续 `push` 和 `tag`，可直接用于 CI 的失败判定。

## 配置说明

- **Python 项目**：在根目录 `pyproject.toml` 中维护 `[project].version` 与依赖；UV 类构建会读写该版本并同步到其它清单。
- **纯前端 / 子包**：在对应 `package.json` 中可使用 `funbuild` 字段（对象或 `true`）扩展行为（例如自定义 `build` 命令、`cleanDirs` 等），具体逻辑见源码中 `NpmFrontendBuild`。
- **Flutter 项目**：在 `pubspec.yaml` 中可使用 `funbuild` 字段自定义 `build` / `install` / `publish` / `cleanDirs`，具体逻辑见源码中 `FlutterBuild`；详见上方「[打包支持的项目类型](#打包支持的项目类型)」里的 Flutter 小节。

构建类型的判定顺序见上方「[打包支持的项目类型](#打包支持的项目类型)」表格及 `src/funbuild/core/registry.py` 中的注册表；无需再使用旧文档中的 `[tool.funbuild]` 等虚构段名。

## 集成组件

- [uv](https://github.com/astral-sh/uv) — 包管理与构建
- [ruff](https://github.com/astral-sh/ruff) — Lint / 格式化（按项目配置使用）
- [typer](https://typer.tiangolo.com/)（`typer-slim`）— CLI 框架
- [aicommits](https://github.com/Nutlope/aicommits) — 默认与 `push` 流水线集成的提交信息生成（以本机 CLI 为准）

## 仓库布局（摘要）

```
funbuild/
├── src/
│   └── funbuild/
│       ├── core/       # 构建注册与各策略实现
│       └── tool/       # 附加工具入口
├── tests/              # 单元测试
├── pyproject.toml
└── README.md
```

## 参与贡献

1. Fork [funbuild](https://github.com/farfarfun/funbuild)
2. 新建分支并提交变更
3. 发起 Pull Request

本地开发示例：

```bash
git clone https://github.com/farfarfun/funbuild.git
cd funbuild
uv pip install -e .
# 或: pip install -e .

# 跑测试 (pytest 已在 dev 依赖组, uv 会自动装)
uv run pytest -q

# 格式化与静态检查
uvx ruff format .
uvx ruff check . --fix
```

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。

## 链接

- [源码仓库](https://github.com/farfarfun/funbuild)
- [PyPI：funbuild](https://pypi.org/project/funbuild/)
- [Issues](https://github.com/farfarfun/funbuild/issues)

## 维护者

- **牛哥** — [niuliangtao@qq.com](mailto:niuliangtao@qq.com)
- **farfarfun** — [farfarfun@qq.com](mailto:farfarfun@qq.com)

若 funbuild 对你有帮助，欢迎点个 Star。

---

## 关于 farfarfun

[farfarfun](https://github.com/farfarfun) 是一个专注于实用工具库的开源组织，
涵盖云存储、数据处理、AI、多媒体与开发工具链等方向。

- 🏠 组织主页：<https://github.com/farfarfun>
- 📦 PyPI：<https://pypi.org/user/niuliangtao/>
- 📧 联系：farfarfun@qq.com

本项目基于 [MIT](LICENSE) 协议开源。
