# funbuild

Python / 前端 / Flutter / Rust 仓库的一键发版工具：按仓库布局自动判定构建策略，串联
版本递增、清单同步、构建、安装校验、发布、提交、打 tag。

```bash
cargo install funbuild
funbuild build        # 完整发版流水线
funbuild push         # 按文件修改时间分批提交并推送
funbuild upgrade      # 只递增版本号并写回各清单
```

也发布在 PyPI（`pip install funbuild`，wheel 里装的是同一个二进制），两条渠道同号。

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
