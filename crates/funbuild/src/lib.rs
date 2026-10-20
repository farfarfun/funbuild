//! funbuild：Python / 前端 / Flutter / Rust 仓库的一键发版工具。
//!
//! 下面的模块一律私有：它们是实现，不是给外部调用的 Rust API，所以 lib 对外只有
//! [`run`]。同一个 crate 内的测试照样能访问私有模块，不需要为了「可测」把它们
//! pub 出去——那等于用 1.6.x 的 semver 给一堆内部结构背书。
//!
//! lib 与 bin 双 target 只为让解析测试能 `use`：bin-only 的 crate 测起来只能真去
//! fork 进程。

mod builders;
mod commit;
mod error;
mod git;
mod latest_deps;
mod manifest;
mod shell;
mod util;
mod version;

use std::ffi::OsString;
use std::path::PathBuf;

use clap::{Parser, Subcommand};

use crate::builders::Builder;
use crate::error::{Error, Result};

#[derive(Parser, Debug)]
#[command(name = "funbuild", version, about = "build tool for \"fun\"")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// 升级版本
    Upgrade {
        /// 指定目标版本号; 不传则沿用旧逻辑自动递增当前版本号
        #[arg(long)]
        version: Option<String>,
    },

    /// 把 latest-packages 里的依赖下界抬到最新版 (不构建、不发布)
    #[command(name = "latest-deps")]
    LatestDeps,

    /// 拉取代码
    Pull,

    /// 推送代码
    Push {
        /// 传 "all" 时, 先依次在每个 submodule 里执行 push, 再 push 当前仓库
        target: Option<String>,
        /// commit 信息; 不传则由 aicommits 依据改动自动生成
        #[arg(long, short)]
        message: Option<String>,
        /// 每个提交包含的最大修改文件数
        #[arg(long, default_value_t = 20, value_parser = clap::value_parser!(u64).range(1..))]
        batch_size: u64,
    },

    /// 安装包
    Install,

    /// 构建发布
    #[command(visible_alias = "release")]
    Build {
        /// 提交时的 commit 信息; 不传则由 aicommits 依据改动自动生成
        message: Option<String>,
        /// 指定发布版本号; 不传则沿用旧逻辑自动递增当前版本号
        #[arg(long)]
        version: Option<String>,
        /// 仅预览子模块工作区的发布计划，不拉取或修改文件
        #[arg(long)]
        dry_run: bool,
        /// 本次只发布这些应用（逗号分隔），仍遵守 exclude-apps；仅用于子模块工作区
        #[arg(long, value_delimiter = ',')]
        apps: Vec<String>,
    },

    /// 清理历史
    #[command(name = "clean-history")]
    CleanHistory,

    /// 清理缓存
    Clean,

    /// 创建标签
    Tag,
}

pub fn run<I, T>(args: I) -> i32
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let cli = match Cli::try_parse_from(args) {
        Ok(cli) => cli,
        // clap 自己把帮助/版本和用法错误打到对的流上，这里只负责转成退出码。
        // 用法错误是 2（与 typer/click 一致），`--help` / `--version` 是 0。
        Err(error) => {
            let _ = error.print();
            return if error.use_stderr() { 2 } else { 0 };
        }
    };

    init_logging();
    match execute(cli.command) {
        Ok(()) => 0,
        Err(error @ Error::NotAGitRepository(_)) => {
            // 裸抛会甩用户一脸细节, 而这只是「跑错目录了」
            eprintln!("错误: {error}");
            1
        }
        Err(error) => {
            log::error!("{error}");
            1
        }
    }
}

/// 日志级别走 `FUNBUILD_LOG` 而不是 `RUST_LOG`。
///
/// funbuild 调的 uv 自己也是 Rust 写的、也读 `RUST_LOG`：共用一个变量的话
/// `RUST_LOG=debug funbuild build` 会把每个 uv 子进程一起调成 debug，funbuild
/// 自己那几行日志直接被 pubgrub 的求解过程淹掉。分开之后 `RUST_LOG` 仍然留给 uv。
fn init_logging() {
    // 重复初始化（同进程里多次调 run，比如单元测试）会返回 Err，忽略即可。
    let _ = env_logger::Builder::from_env(env_logger::Env::new().filter_or("FUNBUILD_LOG", "info"))
        .format_timestamp_secs()
        .format_target(false)
        .try_init();
}

/// 探测构建类型，顺带把工作目录归一化到仓库根。
///
/// 必须归一化：清单探测与构建命令（`rm -rf dist`、`uv build --directory .`）全部
/// 按相对路径解析，从子目录运行时这些路径统统落空，于是静默退化成空构建——退出码
/// 还是 0，看起来像发布成功了，实际什么都没做。
fn builder() -> Result<Box<dyn Builder>> {
    let cwd = std::env::current_dir()?;
    let root: PathBuf = git::repo_root(&cwd)?;
    std::env::set_current_dir(&root)?;
    let builder = builders::detect(root)?;
    // 把判定结果说出来：「为什么我的仓库被当成这个类型」是真实的排查场景
    log::info!("{}: {}", builder.ctx().name, builder.kind());
    Ok(builder)
}

fn execute(command: Command) -> Result<()> {
    match command {
        Command::Upgrade { version } => builder()?.upgrade(version.as_deref()),
        Command::LatestDeps => builder()?.sync_latest_dependencies(),
        Command::Pull => builder()?.pull(),
        Command::Push {
            target,
            message,
            batch_size,
        } => {
            let batch_size = batch_size as usize;
            match target.as_deref() {
                None => builder()?.push(message.as_deref(), batch_size),
                Some("all") => builder()?.push_all(message.as_deref(), batch_size),
                Some(other) => Err(Error::invalid(format!(
                    "push 的位置参数只接受 \"all\", 收到 {other:?}"
                ))),
            }
        }
        Command::Install => builder()?.install(),
        Command::Build {
            message,
            version,
            dry_run,
            apps,
        } => builder()?.build(message.as_deref(), version.as_deref(), dry_run, &apps),
        Command::CleanHistory => builder()?.clean_history(),
        Command::Clean => builder()?.clean(),
        Command::Tag => builder()?.tags(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Command {
        Cli::try_parse_from(args).expect("should parse").command
    }

    #[test]
    fn upgrade_takes_optional_version() {
        assert!(matches!(
            parse(&["funbuild", "upgrade"]),
            Command::Upgrade { version: None }
        ));
        match parse(&["funbuild", "upgrade", "--version", "1.2.3"]) {
            Command::Upgrade { version } => assert_eq!(version.as_deref(), Some("1.2.3")),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn build_takes_positional_message_and_version_option() {
        match parse(&["funbuild", "build", "fix: 修复问题", "--version", "2.0.0"]) {
            Command::Build {
                message,
                version,
                dry_run,
                apps,
            } => {
                assert_eq!(message.as_deref(), Some("fix: 修复问题"));
                assert_eq!(version.as_deref(), Some("2.0.0"));
                assert!(!dry_run);
                assert!(apps.is_empty());
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn build_accepts_workspace_preview_and_comma_separated_apps() {
        match parse(&[
            "funbuild",
            "release",
            "--dry-run",
            "--apps",
            "app-a,app-b",
            "--apps",
            "app-c",
        ]) {
            Command::Build { dry_run, apps, .. } => {
                assert!(dry_run);
                assert_eq!(apps, ["app-a", "app-b", "app-c"]);
            }
            other => panic!("unexpected: {other:?}"),
        }
        assert!(Cli::try_parse_from(["funbuild", "build", "--apps"]).is_err());
    }

    #[test]
    fn release_is_an_alias_of_build() {
        assert!(matches!(
            parse(&["funbuild", "release"]),
            Command::Build { .. }
        ));
    }

    #[test]
    fn push_defaults_to_batch_of_twenty() {
        match parse(&["funbuild", "push"]) {
            Command::Push {
                target,
                message,
                batch_size,
            } => {
                assert_eq!(target, None);
                assert_eq!(message, None);
                assert_eq!(batch_size, 20);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn push_accepts_short_and_long_message_plus_all() {
        for flag in ["-m", "--message"] {
            match parse(&["funbuild", "push", "all", flag, "chore: x"]) {
                Command::Push {
                    target, message, ..
                } => {
                    assert_eq!(target.as_deref(), Some("all"));
                    assert_eq!(message.as_deref(), Some("chore: x"));
                }
                other => panic!("unexpected: {other:?}"),
            }
        }
    }

    #[test]
    fn batch_size_must_be_at_least_one() {
        assert!(Cli::try_parse_from(["funbuild", "push", "--batch-size", "0"]).is_err());
        assert!(Cli::try_parse_from(["funbuild", "push", "--batch-size", "1"]).is_ok());
    }

    #[test]
    fn hyphenated_subcommand_names() {
        assert!(matches!(
            parse(&["funbuild", "latest-deps"]),
            Command::LatestDeps
        ));
        assert!(matches!(
            parse(&["funbuild", "clean-history"]),
            Command::CleanHistory
        ));
        // 下划线形式不该被接受（typer 也是转成连字符的）
        assert!(Cli::try_parse_from(["funbuild", "clean_history"]).is_err());
    }

    #[test]
    fn every_subcommand_from_the_readme_table_parses() {
        for args in [
            vec!["funbuild", "upgrade"],
            vec!["funbuild", "latest-deps"],
            vec!["funbuild", "pull"],
            vec!["funbuild", "push"],
            vec!["funbuild", "install"],
            vec!["funbuild", "build"],
            vec!["funbuild", "release"],
            vec!["funbuild", "tag"],
            vec!["funbuild", "clean"],
            vec!["funbuild", "clean-history"],
        ] {
            assert!(Cli::try_parse_from(&args).is_ok(), "{args:?} 应当可解析");
        }
    }

    #[test]
    fn help_and_version_exit_zero_while_usage_errors_exit_two() {
        assert_eq!(run(["funbuild", "--help"]), 0);
        assert_eq!(run(["funbuild", "--version"]), 0);
        assert_eq!(run(["funbuild", "no-such-command"]), 2);
        // 没有子命令时 clap 打用法到 stderr
        assert_eq!(run(["funbuild"]), 2);
    }

    #[test]
    fn help_does_not_probe_the_repository() {
        // `--help` 在非 git 目录下也必须是 0：探测是延迟的
        let dir = tempfile::tempdir().unwrap();
        let previous = std::env::current_dir().unwrap();
        std::env::set_current_dir(dir.path()).unwrap();
        let code = run(["funbuild", "--help"]);
        std::env::set_current_dir(previous).unwrap();
        assert_eq!(code, 0);
    }
}
