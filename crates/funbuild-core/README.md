# funbuild-core

[funbuild](https://crates.io/crates/funbuild) 的实现层：构建类型探测、版本清单读写、
发版流程编排。CLI 只负责把参数解析出来，别的都在这里。

```rust
use funbuild_core::builders;

let repo = std::env::current_dir()?;
let mut builder = builders::detect(repo)?;
builder.upgrade(None)?;          // 递增版本号并写回各清单
```

## 设计取舍

- **命令是字符串，一律交给 `sh -c`。** 生成的命令串里有 glob（`dist/*/*.whl`）、子
  shell（`(cd x && y)`）、命令替换（`$(ls -t *.tgz | head -n1)`）和 `export PATH=`，
  换成 `Command::args` 这些全部失效。同一批命令用 ` && ` 串进一个 shell 进程，
  所以前一条的 `export` 对后一条有效。
- **改写清单保留排版。** TOML 走 `toml_edit`、JSON 保留键序、pubspec 的 version 行用
  正则替换——发版提交里只该有版本号那一行改动，用户写的注释和多行数组不能被冲掉。
- **探测出错不中断链条，但要留痕。** 单个构建类型判定失败只告警并继续往下试；全部
  落空且期间有清单解析失败时报错，而不是退化成「什么都不做却退出码 0」。
- **构建类型靠组合而非继承。** `MaturinBuild` 持有一个 `UvBuild` 并转发 `ctx()`，
  状态只有一份，不会出现两份会漂移的 name/version。

## 版本

0.x，和 `funbuild` 的 1.6.x 是两条独立的版本线。那些 `pub mod` 之所以 public 是为了
给 CLI crate 复用，并不是按公共 Rust API 设计的——0.x 期间它们还会调整。

## 许可证

MIT
