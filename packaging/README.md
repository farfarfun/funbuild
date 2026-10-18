# 分发 funbuild

funbuild 有五条分发渠道。前两条（apt / brew）靠组织的共享包仓库
[`farfarfun/packages`](https://github.com/farfarfun/packages)，剩下三条由本仓库的
[release.yml](../.github/workflows/release.yml) 直接产出：

| 渠道 | 谁做的 |
| --- | --- |
| `apt install funbuild` | `farfarfun/packages` 拉本仓库 Release 里的 `.deb`，重建签名索引发到 Pages |
| `brew install funbuild` | `farfarfun/packages` 按 `SHA256SUMS` 渲染 formula，提交回它自己的 `Formula/`（那个仓库同时就是 tap） |
| `pip install funbuild` | 本仓库 → PyPI（通用 wheel / sdist）。只提供 `funbuild.tool` Python 兼容模块，不安装命令 |
| `cargo install funbuild` | 本仓库 → crates.io（`crates`） |
| 裸二进制 / tarball | 本仓库 → GitHub Release（`binaries`） |

**funbuild 这边不需要任何 secret，也不需要跨仓库写权限。**共享仓库是拉取模型：它读
自己的 `packages.toml`，从各项目的非草稿非预发布 Release 里下 `*.deb`。funbuild 要做
的只是把资产挂到 Release 上，并在那边的 `packages.toml` 里登记几行。

发完版后新版本什么时候出现在 `apt` 里，取决于共享仓库那边每两小时一次的 cron。想
立刻生效就手动触发它：

```bash
gh workflow run build.yml -R farfarfun/packages
```

## 附件名就是接口

三个渲染器都按名字认，错一个字符那条渠道就静默少一个平台：

| 用途 | 文件名 |
| --- | --- |
| apt | `funbuild_<版本>_amd64.deb`、`funbuild_<版本>_arm64.deb` |
| brew | `funbuild-<target>.tar.gz`，target 取 `{x86_64,aarch64}-{unknown-linux-musl,apple-darwin}` 四个 |
| 校验和 | `SHA256SUMS`，**最后一步**生成，覆盖本次所有附件 |

`.tar.gz` 里是一个可执行文件，名字就是裸 `funbuild`——formula 的 `bin.install` 和
`build-deb.sh` 都按裸名取。tarball 必须在产出二进制的那个 job 里打好：
`upload-artifact` 不保留权限位，裸文件传到下游 job 就成了 0644。

## 两个决定

**`.deb` 写 `Depends: git`。** 二进制本身是 musl 静态链接的、没有库依赖，但 funbuild
的每条命令都要先 `git rev-parse --show-toplevel` 找仓库根，没有 git 连启动探测都过不去。

**`uv` 不写进依赖。** Debian 仓库里没有这个包（它自己走 PyPI / 独立二进制分发），
Homebrew 那边的 formula 渲染器也不支持声明依赖。发 Python 项目的人得自己装，`.deb`
的 Description 和 README 里都写了。只用 funbuild 发前端 / Flutter / Rust 项目的话
不需要它。

## 本地打一个 .deb

```bash
cargo build --release --target x86_64-unknown-linux-musl
bash packaging/build-deb.sh \
  target/x86_64-unknown-linux-musl/release/funbuild 1.6.105 amd64 dist
```
