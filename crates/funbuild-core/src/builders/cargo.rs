use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::builders::{Builder, RepoContext};
use crate::error::{Error, Result};
use crate::manifest::cargo as cargo_manifest;
use crate::manifest::sync;
use crate::shell;
use crate::version::parse_version;

/// 纯 Rust 仓库（Cargo.toml 是唯一版本主源）。
///
/// **不发布**：这一族仓库的发布由推 `v*` tag 触发的 GitHub Actions 完成，funbuild
/// 只管到「版本号 + 本地校验 + 提交 + 打 tag + 推送」为止。
pub struct CargoBuild {
    ctx: RepoContext,
}

impl CargoBuild {
    const MANIFEST: &'static str = "Cargo.toml";
    const LOCK: &'static str = "Cargo.lock";

    /// 这几样在根目录的话，版本主源就不是 Cargo.toml：带 pyproject 的 Rust 仓库是
    /// maturin 项目或「Python 主体 + 附带一个 crate」，两者都以 pyproject 为准。
    ///
    /// 不排除 `VERSION`：Cargo.toml 才是 cargo 和 release.yml 实际读的东西。
    const FOREIGN: [&'static str; 3] = ["pyproject.toml", "package.json", "pubspec.yaml"];

    /// `-w` 只重算 workspace 内的包（就是刚改的那个版本号），其余已在锁文件里的
    /// 依赖一个都不动。裸 `cargo update` 会把所有依赖升到最新兼容版。
    const LOCK_UPDATE: &'static str = "cargo update --workspace";

    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let manifest = repo_path.join(Self::MANIFEST);
        if !manifest.is_file() {
            return Ok(None);
        }
        for foreign in Self::FOREIGN {
            if repo_path.join(foreign).is_file() {
                log::info!("skip CargoBuild: 根目录有 {foreign}, 版本主源不在 Cargo.toml");
                return Ok(None);
            }
        }

        // 清单写坏了不能当成「不是 Rust 仓库」，所以这里的错误往上抛
        let Some(version) = cargo_manifest::read_version(&manifest)? else {
            // 纯虚拟 workspace（版本都在各成员自己手里）。认了就会打出一个不对应
            // 任何 crate 的 tag，而 write_version 根本没东西可写。
            log::warn!(
                "skip CargoBuild: {} 里 [package] 和 [workspace.package] 下都没有字面量 version; \
                 funbuild 的模型是「一个仓库一个版本号」, 认领只会打出不对应任何 crate 的 tag",
                manifest.display()
            );
            return Ok(None);
        };

        if parse_version(&version).is_err() {
            // 版本串格式不对不影响判定——Rust 仓库的证据已经压倒性了。硬报错留给
            // upgrade 里的 parse，那时还没动过 git。
            log::warn!(
                "{} 的 version 解析不了: {version:?}, 仍按 Rust 仓库处理",
                manifest.display()
            );
        }
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        ctx.version = Some(version);
        Ok(Some(Self { ctx }))
    }

    /// 版本号改了之后把 Cargo.lock 一起更新。
    ///
    /// 必须在这里做：`funbuild upgrade` 根本不构建，那条路径下清单和锁文件会分叉，
    /// 然后被下一次 push 原样提交上去；而 `cargo publish --locked` 排在 release
    /// 流程的最后，那时 tag 早推出去了，什么都撤不回来。
    fn sync_lock(&self) -> Result<()> {
        let lock = self.ctx.repo_path.join(Self::LOCK);
        if !lock.is_file() {
            // 仓库自己不带锁文件（库 crate 常把它 gitignore）就别造一个：新生成的
            // 未跟踪文件会被 push 的 `git add -A -f` 一起提交上去。
            log::info!("skip Cargo.lock 同步: {} 不在仓库里", lock.display());
            return Ok(());
        }
        let cwd = self.ctx.cwd();
        match shell::run_checked(&[format!("{} --offline", Self::LOCK_UPDATE)], cwd) {
            Ok(()) => return Ok(()),
            Err(error) => {
                // 冷缓存（刚 clone）或刚加了新依赖时，离线解析不出来
                log::warn!("离线更新 Cargo.lock 失败, 改为联网重试: {error}");
            }
        }
        // 兜底仍带 --workspace，所以只会补齐缺失的条目
        shell::run_checked(&[Self::LOCK_UPDATE.to_string()], cwd)
    }
}

impl Builder for CargoBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "CargoBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        let manifest = self.ctx.repo_path.join(Self::MANIFEST);
        let synced = cargo_manifest::sync_version(&manifest, &version)?;

        // 校验结果而不是只看返回值：行尾注释、单引号、version 行不在目标表下，都会
        // 让替换悄悄落空。这里必须中止——Cargo.toml 是唯一的版本主源，放过去的后果
        // 是 tags() 照样推了 v<新版>，然后 release.yml 的版本核对在 tag 公开之后才失败。
        if !synced || cargo_manifest::read_version(&manifest)?.as_deref() != Some(version.as_str())
        {
            return Err(Error::version_sync(format!(
                "{} 里的 version 没能写成 {version}; 要求 version 单独一行、形如 \
                 version = \"x.y.z\", 且在 [package] 或 [workspace.package] 下",
                manifest.display()
            )));
        }
        // 纯 Rust 仓库里是 no-op（别家清单已被 detect 排除），保留是为了和其它构建类型一致
        sync::sync_all(&self.ctx.repo_path, &version)?;
        self.sync_lock()
    }

    /// 发版前的本地校验，和仓库 ci.yml 里的检查逐字一致，顺序是「最快失败的排前面」。
    ///
    /// 不看 is_org_repo、也不自动 `cargo fmt --all`：本地自动改完再提交等于让这道
    /// 关卡失去意义。
    fn cmd_build(&self) -> Result<Vec<String>> {
        Ok([
            "cargo fmt --all --check",
            "cargo clippy --all-targets -- -D warnings",
            "cargo test",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect())
    }

    /// 不装。`cargo install --path .` 会覆盖掉按正式渠道装的那份。
    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    /// 不发布。crates.io 上的版本号是一次性的，所以发布只能发生在 tag 触发的
    /// workflow 里——那里 `cargo publish` 排在装机验证之后，还会先探一次 crates.io。
    fn cmd_publish(&self) -> Result<Vec<String>> {
        log::info!(
            "skip publish: 纯 Rust 仓库由 tag 触发的 GitHub Actions 发布 (含 cargo publish)"
        );
        Ok(Vec::new())
    }

    /// 不清理。`target/` 刻意不列：build 在 publish 之后会再跑一次清理，于是每次
    /// 发版要付两次冷构建的代价。
    fn cmd_delete(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    /// 没有 Python 分发，返回空表让本机升级那一步变成空操作。
    fn published_distributions(&self) -> Result<BTreeMap<String, PathBuf>> {
        Ok(BTreeMap::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rust_repo(dir: &Path, manifest: &str) {
        std::fs::write(dir.join("Cargo.toml"), manifest).unwrap();
    }

    #[test]
    fn detects_single_crate() {
        let dir = tempfile::tempdir().unwrap();
        rust_repo(
            dir.path(),
            "[package]\nname = \"demo\"\nversion = \"0.3.11\"\n",
        );
        let build = CargoBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("0.3.11"));
    }

    #[test]
    fn yields_to_other_manifests() {
        for foreign in CargoBuild::FOREIGN {
            let dir = tempfile::tempdir().unwrap();
            rust_repo(dir.path(), "[package]\nversion = \"1.0.0\"\n");
            std::fs::write(dir.path().join(foreign), "{}").unwrap();
            assert!(
                CargoBuild::detect(dir.path()).unwrap().is_none(),
                "应让位给 {foreign}"
            );
        }
    }

    #[test]
    fn keeps_version_file_repos() {
        // VERSION 不在排除名单里：Cargo.toml 才是权威
        let dir = tempfile::tempdir().unwrap();
        rust_repo(dir.path(), "[package]\nversion = \"1.0.0\"\n");
        std::fs::write(dir.path().join("VERSION"), "0.0.1\n").unwrap();
        assert!(CargoBuild::detect(dir.path()).unwrap().is_some());
    }

    #[test]
    fn skips_pure_virtual_workspace() {
        let dir = tempfile::tempdir().unwrap();
        rust_repo(dir.path(), "[workspace]\nmembers = [\"crates/a\"]\n");
        assert!(CargoBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn broken_manifest_is_an_error_not_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        rust_repo(dir.path(), "[package\nname =");
        assert!(CargoBuild::detect(dir.path()).is_err());
    }

    #[test]
    fn build_matches_ci_checks() {
        let dir = tempfile::tempdir().unwrap();
        rust_repo(dir.path(), "[package]\nversion = \"1.0.0\"\n");
        let build = CargoBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.cmd_build().unwrap(),
            vec![
                "cargo fmt --all --check",
                "cargo clippy --all-targets -- -D warnings",
                "cargo test"
            ]
        );
        assert!(build.cmd_install().unwrap().is_empty());
        assert!(build.cmd_publish().unwrap().is_empty());
        assert!(build.cmd_delete().unwrap().is_empty(), "target/ 不该被清");
        assert!(build.published_distributions().unwrap().is_empty());
    }

    #[test]
    fn write_version_refuses_when_replacement_misses() {
        let dir = tempfile::tempdir().unwrap();
        // version 在 [dependencies] 下，不是版本主源的位置
        rust_repo(
            dir.path(),
            "[package]\nname = \"demo\"\nversion = \"1.0.0\"\n[dependencies]\n",
        );
        let mut build = CargoBuild::detect(dir.path()).unwrap().unwrap();
        build.ctx_mut().version = Some("1.0.1".into());
        build.write_version().unwrap();

        // 换成一个替换必然落空的清单
        rust_repo(dir.path(), "[package]\nversion.workspace = true\n");
        let error = build.write_version().unwrap_err();
        assert!(matches!(error, Error::ManifestVersionSync(_)), "{error}");
    }

    #[test]
    fn skips_lock_sync_without_lock_file() {
        let dir = tempfile::tempdir().unwrap();
        rust_repo(
            dir.path(),
            "[package]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        let mut build = CargoBuild::detect(dir.path()).unwrap().unwrap();
        // 没有 Cargo.lock 时不该去跑 cargo update，也不该造一个锁文件出来
        build.upgrade(None).unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.0.1"));
        assert!(!dir.path().join("Cargo.lock").exists());
    }
}
