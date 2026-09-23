# Changelog

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
