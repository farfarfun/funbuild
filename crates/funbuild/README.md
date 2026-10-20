# funbuild

Python / 前端 / Flutter / Rust 仓库的一键发版工具：按仓库布局自动判定构建策略，串联
版本递增、清单同步、构建、安装校验、发布、提交、打 tag。

```bash
cargo install funbuild
funbuild build        # 完整发版流水线
funbuild push         # 按文件修改时间分批提交并推送
funbuild upgrade      # 只递增版本号并写回各清单
```

PyPI 上的同名包只保留 `funbuild.tool` 的 Python 兼容模块，不提供命令行；二进制请通过 apt、Homebrew、crates.io 或 GitHub Release 安装。

## 判定的项目类型

按下面的顺序探测仓库根目录，命中第一个即停止：

| 构建类型 | 判定条件 |
| --- | --- |
| `SubmoduleWorkspaceBuild` | 同时有 `apps/` 与 `scripts/funbuild.toml` |
| `UvNpmHybridBuild` | 既是 uv 包又有可构建的 `package.json` |
| `MaturinBuild` | pyproject 声明了 maturin |
| `UVBuild` | `pyproject.toml` 有 `[project]` |
| `PoetryBuild` | `[tool.poetry].version` 有效 |
| `PypiBuild` | 有 `script/__version__.md` |
| `FlutterBuild` | `pubspec.yaml` 显式依赖 Flutter SDK |
| `NpmFrontendBuild` | 有带 build 脚本的 `package.json` |
| `CargoBuild` | 只有 `Cargo.toml`，没有上面那些清单 |
| `VersionFileBuild` | 有纯文本 `VERSION` |
| `EmptyBuild` | 兜底，只记一条告警 |

子模块工作区只发布 `.gitmodules` 中登记的 `apps/<name>` 直接子模块。在 `scripts/funbuild.toml` 中设置 `exclude-apps = ["app-b"]` 可排除应用，`build-order = ["sdk", "backend"]` 可指定优先发布顺序，其余应用按目录名排序。名单按目录名精确匹配，拼错名称会报错；没有待发布应用时不修改版本或打标签。

使用 `funbuild build --dry-run` 查看本地发布计划，`funbuild build --apps sdk,backend` 临时选择应用（仍遵守排除名单和发布顺序）。这两个参数仅用于子模块工作区。实际发布会在拉取后重新计算计划，并汇总成功、失败、未执行和跳过的应用；失败中止但不回滚先前已发布的应用。

## 注意：这是一个组织内部工具

funbuild 服务于 [farfarfun](https://github.com/farfarfun) 的发版约定，有几处是写死的：

- 判定为「本组织仓库」时（remote 指向该组织，或仓库名以 `fun` / `nlt` / `note` 开头），
  会往 `pyproject.toml` 里补该组织的 `authors` / `maintainers` / `urls`，并在构建阶段
  跑一次 `uvx ruff format`。
- Flutter 项目的默认发布步骤调 `funpub upload` 到一个固定的私有仓库名。

所以它拿来直接发别人的包未必合适。各阶段的命令可以在 `package.json` / `pubspec.yaml`
的 `funbuild` 字段里覆盖，详见仓库根目录的 README。

## 系统要求

Linux 或 macOS（生成的是 POSIX shell 命令串）、Git，以及 [uv](https://github.com/astral-sh/uv)
（Python 项目的构建与发布都调它）。

## 许可证

MIT
