# Changelog

## [未发布]

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
