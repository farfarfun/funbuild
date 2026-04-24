# nltbuild

[![PyPI version](https://badge.fury.io/py/nltbuild.svg)](https://badge.fury.io/py/nltbuild)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**nltbuild** 是面向 Python / 混合仓库的构建与发布辅助工具：根据项目结构自动选择构建策略（UV、Poetry、旧式 PyPI 脚本、`package.json` 前端包及混合模式等），串联版本递增、构建、安装校验、发布与 Git 标签等常见流程。

## 特性

- **多构建策略**：按仓库布局自动匹配 `UVBuild`、`PoetryBuild`、`PypiBuild`、`NpmFrontendBuild`、`UvNpmHybridBuild` 等实现，无需手写切换逻辑。
- **版本同步**：以根目录 `pyproject.toml` 的 `[project].version` 为主源时，可将版本同步到仓内其它带 `version` 的 `pyproject.toml` 与 `package.json`（含子目录）。
- **依赖与工具链**：内置对 **uv**、**ruff** 等工具的调用约定；日志通过 **nltlog**，Shell 流程通过 **funshell**。
- **Git 工作流**：`pull` / `push` / `tags` 等与远程协作；`push` 默认在提交阶段使用 **aicommits** 生成说明（需本机已安装该 CLI）。
- **维护命令**：`clean` 与 `clean_history` 会改写 Git 状态或强制重写远程历史，使用前请确认团队规范与备份策略。

## 系统要求

- Python 3.9+
- Git（版本管理与标签推送）
- 若使用 `nltbuild push` 的默认行为：本机需可用 `aicommits`（例如 `npm install -g aicommits` 并按其文档配置）

## 安装

### 从 PyPI 安装

```bash
pip install nltbuild
```

### 从源码安装

```bash
git clone https://github.com/farfarfun/nltbuild.git
cd nltbuild
pip install .
```

使用 [uv](https://github.com/astral-sh/uv) 时，可在克隆后执行 `uv sync` 或 `uv pip install -e .` 进行可编辑安装。

## 使用说明

在项目根目录执行 `nltbuild` 子命令（入口由 `pyproject.toml` 中 `[project.scripts]` 注册为 `nltbuild`）。

### 版本

```bash
# 按内置策略自增并写回各清单文件
nltbuild upgrade
```

> 当前 CLI 未暴露「写入指定版本号」参数；需要固定版本时请直接编辑 `pyproject.toml` 等清单后再提交。

### Git

```bash
nltbuild pull

# 默认：git add -A → aicommits --yes → git push
nltbuild push
```

### 构建与发布

```bash
# 本地构建并安装 wheel、执行发布命令（依具体 Build 实现而定），随后 push 与打标签
nltbuild build

# 仅构建并安装到当前环境（不执行完整 publish + 推送流水线时，请直接调用底层工具，例如 uv build）
nltbuild install
```

`nltbuild build` 在基类实现中会依次触发：`pull` → `upgrade` → 构建/安装/发布/清理 → `push` → `tags`。实际命令序列取决于选中的 Build 类型。

### 维护类（高风险）

```bash
# 清理 Git 缓存与索引（会生成提交）
nltbuild clean

# 清空标签并重写远程 master（破坏性操作，仅限明确需要「清历史」的仓库）
nltbuild clean_history

nltbuild tags
```

## 配置说明

- **Python 项目**：在根目录 `pyproject.toml` 中维护 `[project].version` 与依赖；UV 类构建会读写该版本并同步到其它清单。
- **纯前端 / 子包**：在对应 `package.json` 中可使用 `nltbuild` 字段（对象或 `true`）扩展行为（例如自定义 `build` 命令、`cleanDirs` 等），具体逻辑见源码中 `NpmFrontendBuild`。

构建类型的判定顺序见 `src/nltbuild/core/registry.py` 中的注册表；无需再使用旧文档中的 `[tool.funbuild]` 等虚构段名。

## 集成组件

- [uv](https://github.com/astral-sh/uv) — 包管理与构建
- [ruff](https://github.com/astral-sh/ruff) — Lint / 格式化（按项目配置使用）
- [typer](https://typer.tiangolo.com/)（`typer-slim`）— CLI 框架
- [aicommits](https://github.com/Nutlope/aicommits) — 默认与 `push` 流水线集成的提交信息生成（以本机 CLI 为准）

## 仓库布局（摘要）

```
nltbuild/
├── src/
│   └── nltbuild/
│       ├── core/       # 构建注册与各策略实现
│       └── tool/       # 附加工具入口
├── pyproject.toml
└── README.md
```

## 参与贡献

1. Fork [nltbuild](https://github.com/farfarfun/nltbuild)
2. 新建分支并提交变更
3. 发起 Pull Request

本地开发示例：

```bash
git clone https://github.com/farfarfun/nltbuild.git
cd nltbuild
uv pip install -e .
# 或: pip install -e .

ruff format .
ruff check . --fix
```

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。

## 链接

- [源码仓库](https://github.com/farfarfun/nltbuild)
- [PyPI：nltbuild](https://pypi.org/project/nltbuild/)
- [Issues](https://github.com/farfarfun/nltbuild/issues)

## 维护者

- **牛哥** — [niuliangtao@qq.com](mailto:niuliangtao@qq.com)
- **farfarfun** — [farfarfun@qq.com](mailto:farfarfun@qq.com)

若 nltbuild 对你有帮助，欢迎点个 Star。
