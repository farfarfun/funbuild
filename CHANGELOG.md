# Changelog

## [1.6.72]

### 修复

- README/LICENSE/依赖声明按 SPEC.md 规范补正（Python 版本表述统一为 3.10+、LICENSE 版权行、`typer-slim` 版本下限）。
- 移除 `hybrid.py`/`util.py`/`version_sync.py`/`cli.py` 中过时的 `typing.Optional` 写法，改用 `X | None`。

### 变更

- `.gitignore` 补充 `*.db`、`*.rar`、`.venv/`、`.run/` 等规范要求的忽略规则；提交 `uv.lock` 以保证可复现构建。
