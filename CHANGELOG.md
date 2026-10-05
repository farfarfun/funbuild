# Changelog

## [1.6.96]

### 新增

- `build` 的最后一步会把刚发布的版本装进**正在运行 funbuild 的那个环境**。此前的安装步骤走 `uv pip install`，在仓库目录下装的是 `./.venv`，而用户敲的命令往往装在别处 —— funbuild 自己就是如此：`funbuild` 命令在 py312，发完版命令仍跑旧代码，改动要再发一版才「看起来生效」，实际是永远差一版（连发 1.6.85 / 1.6.87 / 1.6.90 / 1.6.91 四版，py312 一直停在 1.6.87）。
- 只升级该环境里**已经装过**的分发，没装过的不碰：发业务包时不该往命令所在的环境里塞一堆它并不需要的包。多包仓库（`extbuild/` `exts/`）按各自 `[project].name` 逐个判断，分发名取自清单而非目录名。
- 装的是工作区而非 PyPI：索引有几分钟传播延迟，刚发布的版本常常只能拉到上一版（1.6.91 实测）。此刻工作区的代码正是刚发布的那一份，等价且立即可用。
- 排在 `push` / `tag` 之后，失败只记 warning：发 funbuild 时这一步是在替换 funbuild 自己的 site-packages，放到最后才不会留下「已发布但没提交」的半截状态；真失败了版本也已发布、提交和 tag 都在，唯一后果是本机没升级，重跑一条命令即可。

## [1.6.94]

### 修复

- funbuild 自己让 aicommits 生成的提交信息 **100% 不符合 `<类型>: <描述>` 约定**，于是每次发版都带一行 warning。原因是调用时什么格式要求都没传，aicommits 默认 `type=plain`（`~/.aicommits` 的默认值）只输出纯描述、从不带前缀。1.6.93 把校验降为告警是对的（判错的代价是丢描述），但信息既然是 funbuild 让它生成的，格式就该由 funbuild 交代清楚，而不是生成完再告警。现在调用命令固定带上 `--type conventional` 和一句限定类型表的 `--prompt`，类型仍由模型按 diff 判断。
- 只传 `--type conventional` 不够：实测它会产出 conventional 全集里的 `perf(version): cache parsed version strings`，`perf` 不在 SPEC 的类型表内，照样不合规。加上 `--prompt` 后同一个 diff 稳定回到 `refactor:`（连跑三次一致）。
- 不传 `--locale`：描述不限语种，语种仍听用户 `~/.aicommits` 的配置。
- 用户不再需要自己 `aicommits config set type=conventional` —— 命令行参数优先于配置文件，funbuild 不依赖、也不修改用户的全局配置。

## [1.6.93]

### 修复

- aicommits 生成的提交信息此前从未被用上，funbuild 1.6.84 起每一条发版标题都是兜底的 `chore: 更新项目文件`。根因是 aicommits 4.x 的 `--yes` **只在 TTY 下才真的提交**：经 shell 调用时（funbuild 这条路径）它把生成的信息打到 stdout 就退出，退出码仍是 0，暂存内容原地不动。funbuild 用 `run_checked` 调它、丢掉 stdout，于是走进「还有暂存内容」的分支静默返回 False，`push` 接着用兜底信息提交 —— AI 写好的描述只在终端上闪了一下。现在捕获它的 stdout，发现它没提交就拿这条信息自己提交。
- 不传 `message` 时「正文只有 `<think>`」这类思维链/围栏污染照旧清理，但清理之后**不再按格式改写**。

### 变更

- **提交信息校验全部降为告警。** `<类型>: <描述>` 只是组织约定，此前三处（CLI 非 0 退出、`push`/`build` 抛 `ValueError`、aicommits 信息改写）都拿它当硬门槛，而判错一次的代价是把写好的描述永久换成 `chore: 更新项目文件` 或者直接发不了版 —— 这个代价明显大于留下一条不规范的标题。现在信息一律原样提交，不合约定只记一行 warning。
- 撤销 1.6.91 的「缺前缀补 `chore: `、表外类型换成 `chore`」：那仍是在改写信息，猜错就同样丢描述。要让类型准确，把 aicommits 设成 conventional 模式（`aicommits config set type=conventional`），由模型按 diff 判断。

### 废弃

- `ensure_valid_commit_message` 更名为 `warn_invalid_commit_message`（不再抛异常），`coerce_commit_message` 已删除。

## [1.6.91]

### 修复

- aicommits 生成的信息只因缺 `<类型>:` 前缀就被整条丢弃，换成回退信息 `chore: 更新项目文件`。aicommits 的 plain 模式（`~/.aicommits` 里 `type=plain`，也是它的默认值）只输出纯描述、从不带前缀，于是**每一条**都被判非法 —— funbuild 自己 1.6.84 到 1.6.91 的提交标题全是这一句，而那几次 aicommits 实际生成的是「修复 latest-packages 查询逻辑：使用 `--refresh-package` 绕过 uv 缓存…」这类可用信息。当时的修法是缺前缀时补上 `chore: ` 并保留原描述 —— **已在 1.6.93 撤销**：标题全是「更新项目文件」的真正根因是 aicommits 在非 TTY 下根本没提交，补前缀治不到，而改写信息本身又会丢描述。

### 废弃

- 无。

## [1.6.90]

### 变更

- 提交信息校验不再要求描述含中文，只校验类型取 `feat`/`fix`/`docs`/`refactor`/`test`/`chore`，描述不限语种。类型仍受约束（`修复: ...` 这类中文类型词、`build:` 这类表外类型照旧拒绝）。注意这一条并不是「提交标题总是 `chore: 更新项目文件`」的原因 —— 真正的原因是缺类型前缀，见 1.6.91。

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
