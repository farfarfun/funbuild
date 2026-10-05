# Changelog

## [未发布]

### 变更

- 提交信息校验不再要求描述含中文，只校验类型取 `feat`/`fix`/`docs`/`refactor`/`test`/`chore`。原先的中文要求把 aicommits 生成的英文信息一律判为非法，于是每次发版都退回默认信息 `chore: 更新项目文件` —— 1.6.85 到 1.6.89 这五个版本的提交标题全是这一句，看不出每次究竟发了什么。类型仍受约束（`修复: ...` 这类中文类型词、`build:` 这类表外类型照旧拒绝）。

### 废弃

- 无。

## [1.6.87]

### 修复

- `latest-packages` 查到的「最新版本」一直是上一个版本: `uv pip compile` 会用 uv 缓存的 index 快照, 而「刚把上游发出去、紧接着发下游」正是这个功能存在的理由 —— 缓存里存的恰好是上游发布之前的状态。`funmill-dev` 实测: 带缓存查到 `funmill-api==1.0.22`(1ms, 纯缓存命中), 真实最新版是 1.0.26。现在查询带 `--refresh-package <包名>`, 只刷名单里这一个包的元数据, 不用 `--refresh` / `--no-cache` 丢掉整个缓存(那会让每次发版都重新拉一遍所有依赖的元数据)。
- 解析失败时 uv 写在 stderr 里的原因被整条丢掉, 报错只剩一句「无法解析 X 的最新版本」, 看不出是私有源没配、401、包名写错, 还是该包所有版本的 `requires-python` 都不匹配当前解释器。`run_shell(printf=False)` 只回传 stdout, 现在把 stderr 单独重定向到临时文件再读出来, 连同命令、工作目录一起带进 `LatestDependencyError`, 并明确指出私有 index 要配在哪 —— `~/.pypirc` 里的 `repository` 是 `uv publish` 的上传端点, 与解析用的 simple index 不是同一个 URL, funbuild 不会拿它去猜。

### 废弃

- 无。

## [1.6.85]

### 新增

- `latest-packages` 名单里的包, 若在 `[project].dependencies`、`[project.optional-dependencies]`、`[dependency-groups]`(含 `extbuild/` `exts/` 子包)里都还没出现过, 现在会按最新版本追加到根 `pyproject.toml` 的 `[project].dependencies`(该键不存在时一并创建)。此前这种情况是静默跳过的 —— 在 `funflix-api` 里写上 `latest-packages = ["funflix"]` 却还没把 `funflix` 写进 `dependencies` 时, 整条配置不产生任何效果: 包不在依赖里, 既进不了 wheel metadata, 也不会被 `uv lock` 解析。
- 补齐有两处刻意的例外: 包名等于本仓库 `[project].name` 时不补(自己依赖自己会让 `uv` 直接解析失败); 补齐只认根 `pyproject.toml` 的 `[tool.funbuild].latest-packages`, 不认 `scripts/funbuild.toml` 的 `packages` —— 后者是编排仓库「这条链上要发哪些包」的清单, 照搬会给根 `pyproject.toml` 凭空加上一堆它并不依赖的包(抬下界仍照旧读这两个来源)。根 `pyproject.toml` 没有 `[project]` 表时只记告警, 不凭空造出一份缺 `name` / `version` 的残缺元数据。

### 变更

- 自身依赖下界抬到当前最新: `uv>=0.12.23`、`tomlkit>=0.15.1`、`pyyaml>=6.0.3`、`farlog>=1.1.8`、`funshell>=1.0.23`。`funshell` 这一条是实际约束而非例行更新 —— 代码里的 `run_shell(..., cwd=, timeout=)` 是新版本才有的参数, 下界停在 `>=1.0.2` 时新装的环境可以解析到一个没有这些参数的 `funshell`, 一调用就 `TypeError`。
- funbuild 自己配上 `[tool.funbuild].latest-packages = ["farlog", "funshell"]`, 此后这两个组织内部包每次发版自动跟最新。第三方包(`uv` / `tomlkit` / `pyyaml` / `typer-slim`)不进名单: 新版本可能带破坏性变更, 要人看过才抬。
- `sync_latest_dependencies` 改为「先把所有清单读进内存、改完再统一落盘」。「这个包有没有声明过」必须看齐全部清单(在子包里声明了就不该再往根上补一份), 而补齐又要改根文档, 边读边写会把根文件写两次, 且第二次改在一份已经过期的文档上。清单按 `realpath` 去重 —— `UVBuild.toml_paths[0]` 是相对路径 `./pyproject.toml`, 与按仓库根拼出来的路径指向同一个文件。
- 新增 `latest_deps.pyproject_latest_packages()`, 即只来自根 `pyproject.toml` 的那份名单; `latest_packages()` 的两来源取并集行为不变。

### 废弃

- 无。

## [1.6.84]

### 新增

- `[tool.funbuild].latest-packages`：列在里面的依赖，每次发版前会把 `pyproject.toml` 里的版本下界抬到当时最新的已发布版本，从而进入 wheel metadata。此前只有 `UVBuild._cmd_build` 开头的 `rm -rf uv.lock && uv lock` 会取最新，那只影响本仓库构建时的解析结果，发出去的 metadata 仍写着早已过期的下界 —— 下游 `pip install -U funflix-api` 默认 `--upgrade-strategy only-if-needed`，已装的旧 `funflix` 满足旧下界就不会被升上来，于是「每次发版都带最新上游」始终落不了地（`funflix-api` 的下界一路停在 `>=0.1.64`，而线上 `funflix` 已是 1.0.5，就是实例）。
- 最新版本经 `uv pip compile --no-deps` 向 index 查询，因此自动沿用仓库自己 `[[tool.uv.index]]` 配置的私有源；解析不出来时抛 `LatestDependencyError` 中止发布，不会沿用旧下界发出一个钉着过期上游的包。
- 改写只动版本下界（`>=` / `>` / `==` / `~=`），extras、environment marker 以及 `<` / `<=` / `!=` / `===` 这些调用方刻意加的限制原样保留；`name @ url` 直接引用不动。覆盖 `[project].dependencies`、`[project.optional-dependencies]` 与 `[dependency-groups]`，含 `extbuild/` `exts/` 子包。
- `funbuild latest-deps`：只做上述改写，不构建、不发布、不提交，用于在真正发版之前确认配置写对了、私有源也解析得通。
- `<product>-dev` 编排仓库 `scripts/funbuild.toml` 里已有的 `packages` 被当作同一份名单读取，不必重复配置。

### 修复

- 构建类型探测过程中若有清单文件解析失败且最终没有任何构建类型认领，不再静默退化成 `EmptyBuild`（`funbuild build` 什么都不做却以退出码 0 结束，看起来像发布成功了），改为抛 `BuilderDetectionError` 并列出每个失败的 builder 与原因。真正没有任何清单的仓库（纯文档仓库）仍照旧兜底到 `EmptyBuild`。
- `load_toml` 解析失败时抛带文件路径的 `ManifestParseError`；tomlkit 原始异常不带文件名，`extbuild/` `exts/` 多清单仓库下无从定位是哪一个文件。
- `api_route` 按 SPEC §15.1 改为发 `DeprecationWarning` 的兼容入口（此前只是 `api_route = ApiRoute` 的裸别名），指明替代品 `ApiRoute` 与移除版本 2.0；行为完全不变。
- README 命令表与实现对齐：`push --message` / `build message` 的默认值是「不传」而非 `add`，未传时优先交给 aicommits、未安装才回退到 `chore: 更新项目文件`；补上 `upgrade`/`build` 的 `--version` 与 `push all`（此前 README 写的是「CLI 未提供写入指定版本号的参数」）；示例提交信息改成能通过校验的中文描述。
- 「aicommits 装了就用、没装回退到 `message`」的描述写反了：显式传了 `message` 就直接用该值，根本不会调用 aicommits。

### 变更

- `farlog` 依赖下界提升到 `>=1.1.7`（SPEC §2 对新代码的要求）。
- 为 `FlutterBuild.check_type`、`NpmFrontendBuild.check_type`、`util.deep_get` 补齐中文 docstring。
- GitHub topics 由 10 个收敛为 8 个（SPEC §11 要求 5-8 个）。
- `BaseBuild.build` 在 `upgrade` 之后、构建之前新增 `_sync_latest_dependencies()` 钩子（基类为空操作，`UVBuild` / `UvNpmHybridBuild` 覆盖）。必须在构建之前：改的是依赖下界，要进这一次的 wheel metadata 才有意义；改动本身随后由同一次 `push` 提交。
- 包名的 PEP 503 归一化从 `submodule_workspace_build` 挪到 `util.normalize_package_name`，与新模块共用一套规则。

### 废弃

- `funbuild.tool.fastapi.api_route`，请改用 `ApiRoute`，计划 2.0 移除。

## [1.6.82]

### 修复

- `_cmd_delete` 里混进了 `rm -rf uv.lock`，而 `build()` 在 publish 之后还会再跑一次 `_cmd_delete`、紧接着就 push，于是每次发版都把 SPEC §5 要求提交的 `uv.lock` 从仓库里删掉一次（1.6.81 的发版提交 `7055526` 就是实例，删掉了 424 行）。清理只保留构建产物；「删 lock 重新解析依赖」的意图挪到 `UVBuild._cmd_build` 开头，构建前照旧刷新 lock。

### 废弃

- 无。

## [1.6.81]

### 修复

- 提交信息校验曾把 SPEC.md §10 的 `<类型>: <做了什么>` 误读成「连类型也必须是中文」，于是 `funbuild push -m "fix: 修复版本解析"` 这种完全合规的信息被判非法直接抛 `ValueError`；全组织的 push / build 都走这里，等于把发版路径整条堵死。类型现按 SPEC 取 `feat`/`fix`/`docs`/`refactor`/`test`/`chore`（允许 `fix(core): ...` 这类可选 scope），描述仍要求含中文。
- 回退信息与内部维护提交（`clean`、`clean_history`）的类型一并改为合规的 `chore:`。
- 提交信息校验下沉到 `build` / `push_all` 入口：此前只在 `push` 里校验，而 `push` 是发布之后才跑的，非法信息会先把包发上 PyPI 再报错，留下「线上有这个版本、仓库里没有对应提交和 tag」的半截状态。CLI 侧对非法信息给一行中文提示并以退出码 1 结束，不再甩 traceback。
- 版本清单解析失败不再被静默跳过：`pyproject.toml` / `package.json` / `pubspec.yaml` 读不出来时抛 `ManifestVersionSyncError` 中止发布，避免只同步了一部分清单却照常发版。待查清单同时改为只纳入真实存在的根 `pyproject.toml`，使没有 `pyproject.toml` 的仓库（`VERSION` 文件仓库、纯前端、纯 Flutter）不受影响。

### 变更

- 补齐 `UVBuild.__init__`、`config_format` 与各构建类 `_write_version` / `check_type` 的类型标注和中文 docstring；删掉 `EmptyBuild` 里纯转发的 `__init__`。

### 废弃

- 无。

## [1.6.80]

### 新增

- 支持子模块工作区自动构建分发，并完善版本同步与构建流程。

### 修复

- 补齐公开 API 类型标注，修正提交清理和 Git 状态处理失败时的错误传播。

### 变更

- 路由装饰器类统一命名为 `ApiRoute`，保留 `api_route` 兼容别名。

### 废弃

- 无。

## [1.6.72]

### 修复

- README/LICENSE/依赖声明按 SPEC.md 规范补正（Python 版本表述统一为 3.10+、LICENSE 版权行、`typer-slim` 版本下限）。
- 移除 `hybrid.py`/`util.py`/`version_sync.py`/`cli.py` 中过时的 `typing.Optional` 写法，改用 `X | None`。
- 发布凭据改为优先读取 `UV_PUBLISH_*` 环境变量，`~/.pypirc` 仅在环境变量未设置时用于补齐（不再无条件覆盖调用方已导出的环境变量）。
- 为 `base.py`（`__init__`/`upgrade`/`pull`/`push`/`install`/`build`/`clean_history`/`clean`/`tags`）和 `fastapi.py` 的公开方法补齐类型标注与中文 docstring。

### 变更

- `.gitignore` 补充 `*.db`、`*.rar`、`.venv/`、`.run/` 等规范要求的忽略规则；提交 `uv.lock` 以保证可复现构建。
