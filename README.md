# funbuild

[![PyPI version](https://badge.fury.io/py/funbuild.svg)](https://badge.fury.io/py/funbuild)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

funbuild 是一个现代化的 Python 项目构建和管理工具，旨在简化 Python 项目的开发、构建、发布和维护流程。它集成了多种构建工具和最佳实践，为开发者提供一站式的项目管理解决方案。

## ✨ 特性

- 🚀 **多构建工具支持**: 支持 PyPI、Poetry 和 UV 等多种构建方式
- 🔄 **自动化版本管理**: 智能版本升级和发布流程
- 📦 **依赖管理**: 自动处理项目依赖和环境配置
- 🔧 **Git 集成**: 内置 Git 操作，包括拉取、推送和历史清理
- 🤖 **AI 提交信息**: 集成 gcop 自动生成智能提交信息
- 🧹 **项目清理**: 自动清理缓存、构建文件和历史记录
- 📋 **标签管理**: 自动创建和管理 Git 标签
- 🎯 **命令行界面**: 简洁易用的 CLI 工具

## 📋 系统要求

- Python 3.9 或更高版本
- Git（用于版本控制功能）

## 🚀 安装

### 从 PyPI 安装（推荐）

```bash
pip install funbuild
```

### 从源码安装

```bash
git clone https://github.com/farfarfun/funbuild.git
cd funbuild
pip install .
```

## 📖 使用指南

### 基本命令

在项目根目录下，您可以使用以下命令来管理您的构建流程：

#### 版本管理

```bash
# 升级项目版本
funbuild upgrade

# 升级到指定版本
funbuild upgrade --version 1.2.3
```

#### Git 操作

```bash
# 拉取最新代码
funbuild pull

# 推送代码（支持自动生成提交信息）
funbuild push --message "您的提交信息"

# 使用 AI 自动生成提交信息
funbuild push
```

#### 项目构建

```bash
# 安装项目依赖
funbuild install

# 构建并发布项目
funbuild build --message "发布信息"

# 仅构建不发布
funbuild build --no-publish
```

#### 项目维护

```bash
# 清理 Git 历史记录
funbuild clean_history

# 清理构建缓存和临时文件
funbuild clean

# 创建 Git 标签
funbuild tags
```

### 高级用法

#### 配置文件

funbuild 使用 `pyproject.toml` 文件进行配置。您可以在该文件中自定义构建行为：

```toml
[tool.funbuild]
# 构建类型：pypi, poetry, uv
build_type = "uv"

# 自动版本升级策略
version_strategy = "patch"  # major, minor, patch

# 发布前是否运行测试
run_tests = true

# 自定义构建命令
build_commands = [
    "ruff check .",
    "pytest tests/",
]
```

#### 环境变量

```bash
# 设置构建类型
export FUNBUILD_TYPE=uv

# 设置发布仓库
export FUNBUILD_REPOSITORY=https://upload.pypi.org/legacy/

# 启用详细日志
export FUNBUILD_VERBOSE=1
```

## 🔧 集成工具

funbuild 集成了以下优秀的工具：

- **[uv](https://github.com/astral-sh/uv)**: 现代 Python 包管理器
- **[ruff](https://github.com/astral-sh/ruff)**: 快速 Python 代码检查和格式化工具
- **[gcop](https://github.com/farfarfun/gcop)**: AI 驱动的 Git 提交信息生成器
- **[typer](https://typer.tiangolo.com/)**: 现代 CLI 应用框架

## 📁 项目结构

```
funbuild/
├── src/
│   └── funbuild/
│       ├── core/          # 核心构建逻辑
│       ├── shell/         # Shell 命令执行
│       └── tool/          # 工具集成
├── examples/              # 使用示例
├── tests/                 # 测试文件
├── pyproject.toml         # 项目配置
└── README.md             # 项目文档
```

## 🤝 贡献指南

我们欢迎任何形式的贡献！请遵循以下步骤：

1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

### 开发环境设置

```bash
# 克隆仓库
git clone https://github.com/farfarfun/funbuild.git
cd funbuild

# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/

# 代码格式化
ruff format .
ruff check . --fix
```

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 🔗 相关链接

- [GitHub 仓库](https://github.com/farfarfun/funbuild)
- [PyPI 页面](https://pypi.org/project/funbuild/)
- [发布记录](https://github.com/farfarfun/funbuild/releases)
- [问题反馈](https://github.com/farfarfun/funbuild/issues)

## 👥 维护者

- **牛哥** - [niuliangtao@qq.com](mailto:niuliangtao@qq.com)
- **farfarfun** - [farfarfun@qq.com](mailto:farfarfun@qq.com)

## 🙏 致谢

感谢所有为 funbuild 项目做出贡献的开发者和用户！

---

如果您觉得 funbuild 对您有帮助，请给我们一个 ⭐️！

