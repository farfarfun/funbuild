# funbuild

[![PyPI version](https://badge.fury.io/py/funbuild.svg)](https://badge.fury.io/py/funbuild)
[![crates.io](https://img.shields.io/crates/v/funbuild.svg)](https://crates.io/crates/funbuild)
[![Rust](https://img.shields.io/badge/rust-1.82+-orange.svg)](https://www.rust-lang.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**funbuild** 是面向 Python / 混合仓库的构建与发布辅助工具：根据项目结构自动选择构建策略（UV、Poetry、旧式 PyPI 脚本、`package.json` 前端包、Flutter、maturin 及纯 Rust 仓库等），串联版本递增、构建、安装校验、发布与 Git 标签等常见流程。

命令行实现是 **Rust**，通过 apt、Homebrew、crates.io 和 GitHub Release 分发。PyPI 包只保留 `funbuild.tool` 的 Python 兼容模块，不安装 `funbuild` 命令，避免与独立二进制冲突。命令行用法与 1.6.101 之前的纯 Python 版一致。

## 特性

- **多构建策略**：按仓库布局自动匹配 `UVBuild`、`MaturinBuild`、`PoetryBuild`、`PypiBuild`、`NpmFrontendBuild`、`FlutterBuild`、`UvNpmHybridBuild`、`CargoBuild` 等实现，无需手写切换逻辑。Python 之外也覆盖前端、Flutter 和纯 Rust 仓库。
- **版本同步**：以根目录 `pyproject.toml` 的 `[project].version` 为主源时，可将版本同步到仓内其它带 `version` 的 `pyproject.toml`、`package.json` 与 `pubspec.yaml`（含子目录；`pubspec.yaml` 只同步 `major.minor.patch`，`+buildNumber` 保持不变）。改写清单时保留注释与排版，发版提交里只有版本号那一行改动。
- **依赖取最新**：在 `[tool.funbuild].latest-packages` 里列出的依赖，每次发版前会把版本下界抬到当时最新的已发布版本并写回 `pyproject.toml`，使其进入 wheel metadata，下游升级时必定带上最新的上游；名单里的包若还没写进 `dependencies`，会按最新版本补上。
- **零运行时依赖**：只依赖 `uv`（发版流程要调 `uv build` / `uv publish` / `uv lock`）。`tomlkit` / `pyyaml` / `typer` / `farlog` / `funshell` 全部消失。
- **Git 工作流**：`pull` / `push` / `tag` 等与远程协作；`push` 在提交阶段优先用 **aicommits** 生成说明，未安装时自动回退到默认信息。
- **失败即中止**：任一 shell 步骤返回非 0 即以非 0 码退出，构建失败不会继续推送或打标签。
- **维护命令**：`clean` 与 `clean-history` 会改写 Git 状态或强制重写远程历史，使用前请确认团队规范与备份策略。

## 打包支持的项目类型

`funbuild` 运行时会按下表顺序（从上到下）依次探测仓库根目录，命中第一个匹配的类型即停止；判定逻辑见 `crates/funbuild-core/src/builders/mod.rs` 的 `detect()`。

| 优先级 | 构建类型 | 判定条件 | 适用项目 |
| --- | --- | --- | --- |
| 1 | `SubmoduleWorkspaceBuild` | 根目录同时存在 `apps/` 目录与 `scripts/funbuild.toml` | `<product>-dev` 编排仓库：`apps/` 下每个 app 是独立仓库的 git submodule，仓库自身没有可构建产物，见下方专节 |
| 2 | `UvNpmHybridBuild` | 同时满足 `UVBuild` 与 `NpmFrontendBuild` 的判定条件 | 同一仓库里既是 UV 管理的 Python 包、又带前端 `package.json` 的混合项目（一条命令串联两侧构建/安装/发布） |
| 3 | `MaturinBuild` | 满足 `UVBuild` 的条件，且 `build-system.build-backend` 以 `maturin` 开头或存在 `[tool.maturin]` | Rust + Python 混合项目：版本号同时写进 `pyproject.toml` 和 `Cargo.toml`，按 Python backend 构建 wheel，由 tag 触发的 workflow 发布 |
| 4 | `UVBuild` | 根目录存在 `pyproject.toml` 且含 `[project]` 段 | 使用 `uv` / PEP 621 标准 `pyproject.toml` 的现代 Python 包（含 `extbuild/`、`exts/` 子包） |
| 5 | `PoetryBuild` | `pyproject.toml` 存在且 `[tool.poetry].version` 有效 | 使用 Poetry 管理版本与依赖的 Python 项目 |
| 6 | `PypiBuild` | 根目录存在 `script/__version__.md` | 早期遗留的 PyPI 发布脚本项目 |
| 7 | `FlutterBuild` | `pubspec.yaml` 存在且 `dependencies.flutter.sdk == flutter` | Flutter 应用/插件项目 |
| 8 | `NpmFrontendBuild` | 存在 `package.json`（含 `extbuild/` 等子目录的多包场景） | npm / pnpm / yarn 管理的纯前端项目 |
| 9 | `CargoBuild` | 根目录存在 `Cargo.toml`，且**没有** `pyproject.toml` / `package.json` / `pubspec.yaml` | 纯 Rust 仓库（产物是二进制而不是 Python 扩展），见下方专节 |
| 10 | `VersionFileBuild` | 根目录存在纯文本 `VERSION` 文件 | 没有 `pyproject.toml` / `package.json`、无需真正构建的仓库（如 Shell 脚本仓库），仅借用 `upgrade` / `push` / `tag` 的版本与 Git 流程 |
| 11 | `EmptyBuild` | 兜底，永远匹配 | 未识别到任何版本清单的仓库；`upgrade`、`build`、`tag` 均为空操作，仅保留提示日志 |

### `<product>-dev` 提交工作区 (`SubmoduleWorkspaceBuild`)

面向 `apps/<name>` 下每个 app 都是独立仓库 git submodule 的编排仓库（配套 [`submodule-workspace-governance`](https://github.com/farfarfun-skill/service-governance) 技能约定的目录结构）。这个仓库本身没有可构建产物，`scripts/funbuild.toml` 里的 `version` 是分发给所有 app 的共享版本号，而不是每个 app 各自维护。

```toml
# scripts/funbuild.toml
version = "1.0.0"
packages = ["funtrack"]   # 可选：这些依赖在每个 app 构建前会被升级到最新版
```

`funbuild build` 在这类仓库上依次执行：

1. `pull` 当前仓库，递增共享 `version`。
2. 遍历 `apps/` 下每个已初始化的 submodule：跳过结构上是嵌套 `<something>-dev` 工作区的 app（自己也有 `apps/` + `scripts/setup.sh`，由它自己的发布节奏管理，不会被递归进入）；其余 app 依次切到跟踪分支、`git pull`、把 `packages` 里配置的依赖升级到最新版（按 app 目录下存在的清单文件分派：`pyproject.toml` → `uv add <pkg>@latest`；`package.json` → 按 `pnpm-lock.yaml`/`yarn.lock`/默认 npm 自动选择 `pnpm add` / `yarn add` / `npm install --save`；`pubspec.yaml` → `dart pub add <pkg>`），再转发 `funbuild build --version <共享版本号>`。
3. 统一 `push`、打 `v{version}` 标签一次，使所有 submodule 指针更新落成一个提交。

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

### 纯 Rust 项目 (`CargoBuild`)

针对产物是二进制、发布交给 CI 的 Rust 仓库（有 `Cargo.toml`，没有 `pyproject.toml` / `package.json` / `pubspec.yaml`）。Rust + Python 的 maturin 扩展归 `MaturinBuild`，不走这里。

**版本主源是 `Cargo.toml`。** `upgrade` 按 128 进制递增 `[package].version`（没有字面量版本时取 `[workspace.package].version`），改写时只替换行首那一行 `version = "..."`，注释与排版一律不动 —— 一来 `Cargo.toml` 是人维护的文件，二来按 tag 发布的 workflow 普遍用 `grep -m1 '^version = ' Cargo.toml` 取版本去和 tag 比对，版本必须留在这个形状上。`[dependencies]` 里的 version 约束不受影响。

**`Cargo.lock` 会跟着一起更新**，命令是 `cargo update --workspace --offline`（离线失败时退回联网，仍带 `--workspace`）。`--workspace` 只重算工作区自己的包，已锁定的外部依赖一个都不升级，所以发版提交里就只有版本号那一行改动。这一步必须做：锁文件过期的话，`cargo publish --locked` 往往排在发布流程的最后，等它失败时 tag 早就推出去、Release 也建好了。

默认命令：

| 阶段 | 默认命令 |
| --- | --- |
| 构建 | `cargo fmt --all --check` + `cargo clippy --all-targets -- -D warnings` + `cargo test` |
| 安装 | 无（不跑 `cargo install`，那会覆盖掉系统里按正式渠道装的那份） |
| 发布 | 无 —— 见下 |
| 清理 | 无（不碰 `target/`，清掉只会让下次发版多付一次冷构建） |

构建阶段这三条是**发版前的关卡**，与仓库 CI 里的检查逐字一致：任一失败就中止，不会继续提交、打 tag、推送。原因是 tag 一推就触发真实发布，不能把编译不过的版本推上去。本地不自动 `cargo fmt --all` 改代码 —— 自动改完再提交，这道关卡就失去意义了。

**不发布。** `cargo publish` 只应发生在一个地方，就是 `v*` tag 触发的 workflow（那里它排在装机验证之后，还会先探一次 crates.io）；crates.io 上的版本号是一次性的，同一版本只能发一次，yank 也只是不让新依赖解析到它。所以 `funbuild build` 在 Rust 仓库里做完「校验 → 提交 → 打 tag → 推送」就收手，剩下的交给 CI。

## 系统要求

- Linux 或 macOS。命令串是 POSIX shell（`rm -rf`、子 shell、`$(...)`）；PyPI 上的兼容模块是跨平台纯 Python 包
- Python 3.12+ 仅供 PyPI 兼容模块使用；独立命令本身不需要 Python
- Git（版本管理与标签推送）
- 可选：`aicommits`（`npm install -g aicommits`，需 4.x）。只有在**不传** `message` 时才会调用它生成提交信息；没装或调用失败则回退到默认信息 `chore: 更新项目文件`。显式传了 `message` 就直接用该值，不走 aicommits。不影响流程。无需额外配置：funbuild 调用时自带 `--type conventional` 和类型表约束，不依赖 `~/.aicommits` 里的 `type`（它默认是 `plain`，只输出纯描述）。描述语种不管，仍听你自己的 `locale`。
- Flutter 项目需要本机装好 `flutter` 命令并加入 `PATH`；`funbuild` 本身只负责拼装 `flutter` 命令并不校验其可用性。
- Flutter 项目的默认发布步骤需要安装 [`funpub`](https://pypi.org/project/funpub/)（`pip install funpub`），并提前用 `funsecret` 配置好 `funpackage` 仓库的凭据，否则 `funpub upload` 会失败。

## 安装

### Python 兼容模块

```bash
pip install funbuild
```

这个包只提供 `funbuild.tool`，不会安装 `funbuild` 命令。使用命令行请选以下安装方式。

### 从 crates.io 安装

```bash
cargo install funbuild
```

### apt（Debian / Ubuntu）

funbuild 没有进 Debian 官方档案，所以要先加一次 farfarfun 的包源。加过之后
`apt install` / `apt upgrade` 就和系统自带的包一样：

```bash
curl -fsSL https://farfarfun.github.io/packages/farfarfun.gpg \
  | sudo tee /usr/share/keyrings/farfarfun.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/farfarfun.gpg] https://farfarfun.github.io/packages/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/farfarfun.list
sudo apt update && sudo apt install funbuild
```

amd64 和 arm64 都有。包里是静态链接的二进制（只 `Depends: git`），Debian 12 /
Ubuntu 22.04 这类较老的发行版同样能装。源是组织共用的
([farfarfun/packages](https://github.com/farfarfun/packages))，加一次之后别的
farfarfun 工具也能直接 `apt install`。

### Homebrew（macOS / Linux）

同理，先加一次 tap：

```bash
brew tap farfarfun/packages https://github.com/farfarfun/packages
brew trust farfarfun/packages
brew install funbuild
```

`tap` 那行要把 URL 写出来，因为 `brew tap a/b` 默认去找 `github.com/a/homebrew-b`，
而那个仓库同时还是 apt 源，不叫 `homebrew-packages`。`trust` 那行也不能省：Homebrew
6.0 起第三方 tap 默认不可信。

### 裸二进制

[Release](https://github.com/farfarfun/funbuild/releases/latest) 里有各平台的独立
二进制和 tarball，`SHA256SUMS` 覆盖全部附件：

```bash
curl -fsSLO https://github.com/farfarfun/funbuild/releases/latest/download/funbuild-x86_64-unknown-linux-gnu
chmod +x funbuild-x86_64-unknown-linux-gnu && sudo mv $_ /usr/local/bin/funbuild
```

> **命令行安装渠道不会帮你装 `uv`。** 发 Python 项目要用它（`uv build` /
> `uv publish` / `uv lock`），需自行安装。只发前端 / Flutter / Rust 项目的话不需要。

### 从源码安装

从源码安装 Python 兼容模块：

```bash
git clone https://github.com/farfarfun/funbuild.git
cd funbuild
pip install .          # 只安装 funbuild.tool，不生成命令
```

从源码构建命令需要 Rust 工具链（1.82+）：

```bash
cargo build --release  # 产物在 target/release/funbuild
```

> 修改命令行代码后运行 `cargo build`，用 `target/debug/funbuild` 测试。

## 命令一览

在项目根目录执行（先通过上述二进制渠道安装 `funbuild`）：

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `upgrade` | `--version`（默认不传＝自动递增） | 版本自增（或写入指定版本号）并写回各清单文件 |
| `latest-deps` | — | 把 `latest-packages` 里的依赖下界抬到最新版（不构建、不发布，用于单独验证配置） |
| `pull` | — | `git pull` |
| `push` | `target`（位置参数，仅接受 `all`）<br>`--message` / `-m`（默认不传）<br>`--batch-size`（默认 `20`） | 按文件修改时间从旧到新分批提交，最后统一推送；传 `all` 时先依次 push 每个 submodule |
| `install` | — | 构建 + 安装到当前环境 + 清理产物 |
| `build` | `message`（位置参数，默认不传）<br>`--version`（默认不传＝自动递增） | 完整发布流水线，见下 |
| `release` | 同 `build` | `build` 的别名，行为完全一致 |
| `tag` | — | 打 `v{version}` 标签并推送 |
| `clean` | — | 重建 Git 索引以应用新的 `.gitignore`（会产生一次提交） |
| `clean-history` | — | **破坏性**：删除全部标签与提交历史并强推远程 |

> 子命令名用连字符，因此是 `clean-history` 而非 `clean_history`；`release` 是 `build` 的别名。

> `--message` / `message` 不传时为 `None`：此时优先交给 aicommits 生成提交信息，未安装 aicommits 才回退到默认信息 `chore: 更新项目文件`（定义在 `crates/funbuild-core/src/commit.rs` 的 `DEFAULT_MESSAGE`）。aicommits 的 `--yes` 只在 TTY 下才真的提交，funbuild 会捕获它的输出、发现它没提交就拿这条信息自己提交；调用时一并把 `<类型>: <描述>` 的格式和类型表交代给它，所以自动生成的信息本身就是合规的。
>
> 显式传入的信息建议也写成 SPEC §10 的 `<类型>: <描述>`（类型取 `feat`/`fix`/`docs`/`refactor`/`test`/`chore`，描述不限语种），但这只是**约定**：不合约定只会记一行 warning，信息照原样提交，既不中止流程也不改写成兜底信息。

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

需要写入指定版本号（而非自动递增）时，给 `upgrade` 或 `build` 传 `--version`：

```bash
funbuild upgrade --version 2.0.0
```

### Git

```bash
funbuild pull

# 默认每 20 个文件一次提交
funbuild push

# 自定义每个提交的文件数
funbuild push --batch-size 50

# 指定提交信息（未安装 aicommits 时生效）；注意这里是选项而非位置参数。
# 建议写成 `<类型>: <描述>`，不合约定只告警、不拦。
funbuild push --message "fix: 修复拼写"

# 先依次 push 每个 submodule，再 push 当前仓库
funbuild push all
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

`build` 依次触发：`pull` → `upgrade` → 抬依赖下界 → 清理 → 构建 → 安装校验 → 发布 → 清理 → `push` → `tag` → 本机升级。实际命令序列取决于选中的 Build 类型。任一步失败会立即中止，不会继续 push 或打标签。

> 最后一步「本机升级」把刚发布的版本装进**正在运行 funbuild 的那个环境**（命令所在的 Python，不是项目的 `.venv` —— 安装校验那一步装的才是 `.venv`）。只升级该环境里已经装过的分发，没装过的不碰，所以发业务包不会往命令环境里塞东西。装的是工作区而非 PyPI（索引有几分钟传播延迟）。这一步失败只记 warning：版本已发布、提交和 tag 都在，重跑一条命令即可。

### 让某个依赖每次发版都取最新

组织内部包之间（如 `funflix-api` 依赖 `funflix`）常常希望每次发版都带上最新的上游。只靠发版时的 `rm -rf uv.lock && uv lock` 是不够的：那只影响本仓库构建时解析到的版本，发布出去的 wheel metadata 仍写着 `pyproject.toml` 里那个早已过期的下界，而下游 `pip install -U funflix-api` 默认 `--upgrade-strategy only-if-needed`，已装的旧 `funflix` 满足旧下界就不会被升上来。

在被依赖方的仓库里声明：

```toml
# funflix-api/pyproject.toml
[project]
dependencies = ["funflix>=1.0.0"]

[tool.funbuild]
latest-packages = ["funflix"]
```

此后每次 `funbuild build` 都会在构建之前：向 index 查询 `funflix` 当前最新版本（走 `uv pip compile --no-deps --refresh-package funflix`，因此自动沿用本仓库 `[[tool.uv.index]]` 配置的私有源，并绕过 uv 的 index 缓存——上游往往是几秒前才发出去的），把 `dependencies` 里的下界改写为 `funflix>=<最新版>` 并写回 `pyproject.toml`，改动随本次发布的 `push` 一起提交。`[project.optional-dependencies]` 与 `[dependency-groups]`（含 `extbuild/` `exts/` 子包的 `pyproject.toml`）同样覆盖。

名单里的包如果在上述任何一处依赖声明里都还没出现过，会按最新版本追加到根 `pyproject.toml` 的 `[project].dependencies`（`dependencies` 键不存在时一并创建）：

```toml
# 改写前
[project]
name = "funflix-api"
dependencies = ["requests>=2"]

[tool.funbuild]
latest-packages = ["funflix"]

# 改写后
dependencies = ["requests>=2", "funflix>=1.9.0"]
```

不补上这条配置就等于白写：包不在依赖里，既进不了 wheel metadata，也不会被 `uv lock` 解析。两点例外：

- **本仓库自己的包名不补**——自己依赖自己会让 `uv` 直接解析失败。
- **只认根 `pyproject.toml` 里的那份名单**。`<product>-dev` 编排仓库 `scripts/funbuild.toml` 的 `packages` 仍会被用来抬下界，但不会被补进 `dependencies`：那是「这条链上要发哪些包」的清单，不是「本仓库依赖哪些包」，照搬会给编排仓库凭空加上一堆它并不依赖的包。根 `pyproject.toml` 没有 `[project]` 表时同样不补，只记一条告警——凭空造出 `[project]` 只会生成一份缺 `name` / `version` 的残缺元数据。

改写只动版本下界，调用方刻意写下的其它信息一字不动：

| 原声明 | 最新版为 `1.9.0` 时 |
| --- | --- |
| `funflix` | `funflix>=1.9.0` |
| `funflix>=1.0.0` | `funflix>=1.9.0` |
| `funflix[all]>=1.0,<2` | `funflix[all]>=1.9.0,<2` |
| `funflix>=1.0,!=1.5.0` | `funflix>=1.9.0,!=1.5.0` |
| `funflix>=1.0; python_version>="3.11"` | `funflix>=1.9.0; python_version>="3.11"` |
| `funflix @ https://example.com/funflix.whl` | 不改动（来源由调用方显式指定，不是 index 上的版本） |

即 `>=` / `>` / `==` / `~=` 跟着最新版走，`<` / `<=` / `!=` / `===` 原样保留。`<product>-dev` 编排仓库无需重复配置，`scripts/funbuild.toml` 里已有的 `packages` 会被当作同一份名单读取。

要在真正发版之前确认配置写对了、私有源也解析得通：

```bash
funbuild latest-deps   # 只改写 pyproject.toml，不构建、不发布、不提交
```

几点需要注意：

- **约束里不要留上界**。写成 `funflix>=1.0,<2` 时，`<2` 会被保留，下游永远拿不到 2.x。要「永远最新」就只写 `funflix` 或 `funflix>=x`。
- **发布顺序由调用方保证**。`funflix` 必须先发完，`funflix-api` 才能解析到它的新版本。两者本来就该一起发时，更合适的做法是建一个 `<product>-dev` 编排仓库交给 `SubmoduleWorkspaceBuild`，一条 `funbuild build` 按顺序发完整条链。
- **私有 index 必须让 uv 看得见**。查询走 `uv pip compile`，它只认 uv 自己的 index 配置：仓库 `pyproject.toml` 里的 `[[tool.uv.index]]`，或 `~/.config/uv/uv.toml` 里的全局配置（凭据可放 `~/.netrc`）。`~/.pypirc` **不算**——那里的 `repository` 是 `uv publish` 用的上传端点，与解析用的 simple index 不是同一个 URL，funbuild 不会拿它去猜。只发在私有源上的包，在没配 index 的仓库里会解析失败。

  ```toml
  [[tool.uv.index]]
  name = "packages-pypi"
  url = "https://packages.aliyun.com/<id>/pypi/<repo>"
  ```

- 解析不出最新版本（私有源没配或不可达、包名写错、该包所有版本的 `requires-python` 都不匹配当前解释器等）时会抛 `LatestDependencyError` 直接中止发布，不会沿用旧下界继续发出一个钉着过期上游的包。报错里带上 uv 自己的输出、工作目录和命令，便于直接定位。

> Poetry 的 `[tool.poetry.dependencies]` 表形式暂不在覆盖范围内，目前只处理 PEP 621 的 `[project]` 依赖数组与 PEP 735 的 `[dependency-groups]`。

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

所有 shell 步骤经 `run_checked()` 执行：命令用 ` && ` 串成一条交给同一个 `sh -c`，任一条退出码非 0 即中止，进程以非 0 码退出。这意味着 `funbuild build` 在构建或发布失败时不会再继续 `push` 和 `tag`，可直接用于 CI 的失败判定。

退出码：`0` 成功、`1` 运行失败、`2` 用法错误（参数写错、子命令不存在）。

## 日志

进度与告警打到 **stderr**，被调用的构建命令的输出直通 stdout，两者分流。用
`FUNBUILD_LOG` 调级别（`FUNBUILD_LOG=debug funbuild build` 会把每条执行的 shell
命令也打出来）。

> 刻意不用 `RUST_LOG`：funbuild 调的 uv 自己也是 Rust 写的、也读这个变量，共用一个
> 的话 funbuild 那几行日志会被 uv 的依赖求解过程淹掉。`RUST_LOG` 仍然留给 uv。

> 1.6.101 及之前的版本会在**每个被操作的仓库**里落一份 `logs/funbuild.log`。那些文件
> 是未跟踪的，`push` 的 `git add -A -f` 会把它们一起提交上去。现在不再写文件日志。

## 配置说明

- **Python 项目**：在根目录 `pyproject.toml` 中维护 `[project].version` 与依赖；UV 类构建会读写该版本并同步到其它清单。可选的 `[tool.funbuild].latest-packages` 用于指定「每次发版取最新」的依赖，见上方「[让某个依赖每次发版都取最新](#让某个依赖每次发版都取最新)」。
- **纯前端 / 子包**：在对应 `package.json` 中可使用 `funbuild` 字段（对象或 `true`）扩展行为（例如自定义 `build` 命令、`cleanDirs` 等），具体逻辑见 `crates/funbuild-core/src/builders/npm.rs`。
- **Flutter 项目**：在 `pubspec.yaml` 中可使用 `funbuild` 字段自定义 `build` / `install` / `publish` / `cleanDirs`，具体逻辑见 `crates/funbuild-core/src/builders/flutter.rs`；详见上方「[打包支持的项目类型](#打包支持的项目类型)」里的 Flutter 小节。
- **纯 Rust 项目**：版本主源是 `Cargo.toml` 的 `[package].version`（或 `[workspace.package].version`），没有可配置项 —— 发版前的三项检查与发布边界都是固定的，详见上方「[纯 Rust 项目 (`CargoBuild`)](#纯-rust-项目-cargobuild)」小节。

构建类型的判定顺序见上方「[打包支持的项目类型](#打包支持的项目类型)」表格及 `crates/funbuild-core/src/builders/mod.rs` 的 `detect()` —— 构建类型由仓库布局自动探测，不需要也无法在配置里指定。`[tool.funbuild]` 目前只认 `latest-packages` 一个键。

## 集成组件

- [uv](https://github.com/astral-sh/uv) — 包管理与构建（唯一的运行时依赖）
- [ruff](https://github.com/astral-sh/ruff) — Lint / 格式化（组织仓库的构建阶段会跑一次）
- [clap](https://docs.rs/clap) — CLI 解析
- [toml_edit](https://docs.rs/toml_edit) — 保留注释与排版的 TOML 改写
- [aicommits](https://github.com/Nutlope/aicommits) — 默认与 `push` 流水线集成的提交信息生成（以本机 CLI 为准）

## 仓库布局（摘要）

Rust 看 `crates/`，Python 看 `src/`；根目录是纯虚拟 Cargo 工作区（没有 `[package]`），这样根目录下的 `cargo test` 照常可用。

```
funbuild/
├── crates/funbuild/        # 唯一的 crate：lib + [[bin]] funbuild
│   └── src/
│       ├── main.rs         # 入口，只转发退出码
│       ├── lib.rs          # clap CLI；对外只暴露 run()，其余模块全私有
│       ├── builders/       # 各构建类型与探测链
│       ├── manifest/       # pyproject / package.json / pubspec / Cargo.toml 的读写
│       ├── commit.rs       # 提交信息校验与 aicommits
│       ├── git.rs          # 仓库状态
│       ├── latest_deps.rs
│       └── shell.rs        # 命令执行
├── src/funbuild/tool/      # 历史遗留的纯 Python 工具（与发版无关）
├── scripts/differential.py # 与改造前的 Python 版做行为差分
├── Cargo.toml
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

# 跑测试
cargo test --workspace

# 格式化与静态检查（CI 以 -D warnings 跑 clippy）
cargo fmt --all
cargo clippy --workspace --all-targets -- -D warnings

# 和改造前的纯 Python 版做行为差分：两边在 15 种仓库布局上各跑一次 upgrade，
# 比对写进清单的数据。改动清单读写逻辑后务必跑一次。
cargo build
python3 scripts/differential.py
```

Python 侧只剩 `src/funbuild/tool`（历史遗留，与发版无关），用 `uvx ruff` 检查。

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。

## 链接

- [源码仓库](https://github.com/farfarfun/funbuild)
- [PyPI：funbuild](https://pypi.org/project/funbuild/)
- [crates.io：funbuild](https://crates.io/crates/funbuild)
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
