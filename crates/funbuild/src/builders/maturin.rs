use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

use crate::builders::uv::UvBuild;
use crate::builders::{Builder, RepoContext};
use crate::error::Result;
use crate::manifest::cargo as cargo_manifest;
use crate::manifest::toml as toml_manifest;
use crate::shell;

/// 去掉 `.` 与 `..`，不要求路径真实存在（canonicalize 要求存在）。
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// maturin (PyO3) 项目。与 UVBuild 的差异只有三处：构建用 maturin、不在本地发布、
/// 版本号还要写进 Cargo.toml。
///
/// 持有一个 UvBuild 而不是继承：状态只存在内层那一份，`ctx()` 转发过去。
pub struct MaturinBuild {
    uv: UvBuild,
}

impl MaturinBuild {
    /// pyproject 声明了 maturin 即认领。
    ///
    /// 必须排在 UVBuild 之前探测：UVBuild 只要求存在 `[project]`，会把 maturin
    /// 项目一并认领走，然后跑本地 `uv publish`。
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let Some(uv) = UvBuild::detect(repo_path)? else {
            return Ok(None);
        };
        let doc = toml_manifest::load(&uv.toml_paths[0])?;
        let declared = toml_manifest::dig_str(&doc, &["build-system", "build-backend"])
            .is_some_and(|backend| backend.trim().starts_with("maturin"));
        if declared || toml_manifest::has_table(&doc, &["tool", "maturin"]) {
            return Ok(Some(Self { uv }));
        }
        Ok(None)
    }

    /// `pkg_dir` 下 maturin 要用的 Cargo.toml。
    ///
    /// 读 `[tool.maturin].manifest-path`（相对 pkg_dir 解析），默认 pkg_dir/Cargo.toml。
    /// 虚拟工作区必须显式配这一项，否则 maturin 会对着根清单报 `missing field package`。
    fn cargo_manifest_path(&self, pkg_dir: &str) -> PathBuf {
        let pkg_dir_abs = normalize(&self.uv.ctx.repo_path.join(pkg_dir));
        let rel = toml_manifest::load(pkg_dir_abs.join("pyproject.toml"))
            .ok()
            .and_then(|doc| {
                toml_manifest::dig_nonempty(&doc, &["tool", "maturin", "manifest-path"])
            })
            .unwrap_or_else(|| "Cargo.toml".to_string());
        normalize(&pkg_dir_abs.join(rel))
    }

    /// 需要同步版本号的 Cargo.toml，从扩展 crate 的清单一路回溯到包目录。
    ///
    /// 回溯是必须的：扩展 crate 一般写 `version.workspace = true`，字面量版本号在
    /// workspace 根的 `[workspace.package]` 里。只改 crate 清单会一个字都改不动，
    /// 而 pyproject 那边的版本号已经写进去了——wheel 和 crate 就此分叉。
    ///
    /// 只走**祖先**目录，不扫整个 workspace：兄弟 crate 可能有意维护独立的版本线。
    fn cargo_manifest_paths(&self) -> Vec<PathBuf> {
        let mut paths: Vec<PathBuf> = Vec::new();
        for pkg_dir in self.uv.buildable_pkg_dirs() {
            let stop = normalize(&self.uv.ctx.repo_path.join(&pkg_dir));
            let manifest = self.cargo_manifest_path(&pkg_dir);
            let mut directory = manifest.parent().unwrap_or(&stop).to_path_buf();
            loop {
                let candidate = directory.join("Cargo.toml");
                if candidate.exists() && !paths.contains(&candidate) {
                    paths.push(candidate);
                }
                let parent = directory.parent().map(Path::to_path_buf);
                match parent {
                    Some(parent) if directory != stop && parent != directory => directory = parent,
                    _ => break,
                }
            }
        }
        paths
    }
}

impl Builder for MaturinBuild {
    fn ctx(&self) -> &RepoContext {
        self.uv.ctx()
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        self.uv.ctx_mut()
    }

    fn kind(&self) -> &'static str {
        "MaturinBuild"
    }

    /// 写 pyproject 之外，还要写 Cargo.toml。
    fn write_version(&mut self) -> Result<()> {
        self.uv.write_version()?;
        let version = self.ctx().version.clone().unwrap_or_default();
        let paths = self.cargo_manifest_paths();
        // 不短路：any() 会在第一个成功的清单上停下，后面的就不写了
        let mut synced = Vec::with_capacity(paths.len());
        for path in &paths {
            synced.push(cargo_manifest::sync_version(path, &version)?);
        }
        if !paths.is_empty() && !synced.iter().any(|ok| *ok) {
            // 版本不同步的后果是 wheel metadata 和 crate 版本对不上，而这个错误
            // 只有在发布之后才会被发现。
            log::warn!(
                "{} 里都没有找到可同步的 version 字段, 版本号未写入",
                paths
                    .iter()
                    .map(|p| p.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
        Ok(())
    }

    /// 构建命令：maturin build --release。
    ///
    /// 不跑 `uv lock`：maturin 项目的依赖解析在 Cargo.lock 里，而那个文件要进仓库、
    /// 由 CI 的 `--locked` 校验，不能在每次发版时删掉重建。
    fn cmd_build(&self) -> Result<Vec<String>> {
        let mut result = Vec::new();
        if self.ctx().is_org_repo() {
            result.push("uvx ruff format".to_string());
            result.push("uvx ruff clean".to_string());
        }
        for pkg_dir in self.uv.buildable_pkg_dirs() {
            let out_dir = self
                .ctx()
                .repo_path
                .join(super::uv::bundle_out_dir(&pkg_dir))
                .display()
                .to_string();
            let manifest = self.cargo_manifest_path(&pkg_dir).display().to_string();
            result.push(format!("rm -rf {}", shell::quote(&out_dir)));
            result.push(format!(
                "maturin build --release --manifest-path {} --out {}",
                shell::quote(&manifest),
                shell::quote(&out_dir)
            ));
        }
        Ok(result)
    }

    /// 不在本地发布——交给 tag 触发的 GitHub Actions。
    ///
    /// 本地 `uv publish` 只能上传一个本机平台的 wheel，其他平台 pip install 会退化到
    /// sdist 现场编译，要求用户装 Rust 工具链。
    fn cmd_publish(&self) -> Result<Vec<String>> {
        log::info!("skip publish: maturin 项目由 tag 触发的 GitHub Actions 发布多平台 wheel");
        Ok(Vec::new())
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        self.uv.cmd_install()
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        self.uv.cmd_delete()
    }

    fn sync_latest_dependencies(&mut self) -> Result<()> {
        self.uv.sync_latest_dependencies()
    }

    fn published_distributions(&self) -> Result<BTreeMap<String, PathBuf>> {
        self.uv.published_distributions()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// funget / funbuild 那种布局：纯虚拟 workspace + crates/ 下的扩展 crate。
    fn virtual_workspace(dir: &Path, crate_dir: &str) {
        std::fs::write(
            dir.join("pyproject.toml"),
            format!(
                "[build-system]\nrequires = [\"maturin>=1.7,<2\"]\nbuild-backend = \"maturin\"\n\n\
                 [project]\nname = \"demo\"\nversion = \"1.2.0\"\n\n\
                 [tool.maturin]\nmanifest-path = \"{crate_dir}/Cargo.toml\"\npython-source = \"src\"\n"
            ),
        )
        .unwrap();
        std::fs::write(
            dir.join("Cargo.toml"),
            format!("[workspace]\nmembers = [\"{crate_dir}\"]\n\n[workspace.package]\nversion = \"1.2.0\"\n"),
        )
        .unwrap();
        std::fs::create_dir_all(dir.join(crate_dir)).unwrap();
        std::fs::write(
            dir.join(crate_dir).join("Cargo.toml"),
            "[package]\nname = \"demo-py\"\nversion.workspace = true\n",
        )
        .unwrap();
    }

    #[test]
    fn detects_via_build_backend() {
        let dir = tempfile::tempdir().unwrap();
        virtual_workspace(dir.path(), "crates/demo-py");
        assert!(MaturinBuild::detect(dir.path()).unwrap().is_some());
    }

    #[test]
    fn detects_via_tool_maturin_table_alone() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n\n[tool.maturin]\nstrip = true\n",
        )
        .unwrap();
        assert!(MaturinBuild::detect(dir.path()).unwrap().is_some());
    }

    #[test]
    fn plain_uv_project_is_not_maturin() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[build-system]\nrequires = [\"hatchling\"]\nbuild-backend = \"hatchling.build\"\n\n[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        )
        .unwrap();
        assert!(MaturinBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn honours_manifest_path_and_walks_up_to_workspace_root() {
        let dir = tempfile::tempdir().unwrap();
        virtual_workspace(dir.path(), "crates/demo-py");
        let build = MaturinBuild::detect(dir.path()).unwrap().unwrap();

        assert_eq!(
            build.cargo_manifest_path("."),
            dir.path().join("crates/demo-py/Cargo.toml")
        );
        // 回溯链：扩展 crate -> workspace 根（中间的 crates/ 没有清单，跳过）
        assert_eq!(
            build.cargo_manifest_paths(),
            vec![
                dir.path().join("crates/demo-py/Cargo.toml"),
                dir.path().join("Cargo.toml"),
            ]
        );
    }

    #[test]
    fn defaults_to_cargo_toml_beside_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n\n[tool.maturin]\nstrip = true\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("Cargo.toml"),
            "[package]\nversion = \"1.0.0\"\n",
        )
        .unwrap();
        let build = MaturinBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.cargo_manifest_path("."),
            dir.path().join("Cargo.toml")
        );
    }

    #[test]
    fn version_lands_in_both_pyproject_and_workspace_cargo() {
        let dir = tempfile::tempdir().unwrap();
        virtual_workspace(dir.path(), "crates/demo-py");
        let mut build = MaturinBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("1.3.0")).unwrap();

        assert!(std::fs::read_to_string(dir.path().join("pyproject.toml"))
            .unwrap()
            .contains("version = \"1.3.0\""));
        // 字面量版本在 workspace 根，crate 自己写的是 version.workspace = true
        assert!(std::fs::read_to_string(dir.path().join("Cargo.toml"))
            .unwrap()
            .contains("version = \"1.3.0\""));
        assert!(
            std::fs::read_to_string(dir.path().join("crates/demo-py/Cargo.toml"))
                .unwrap()
                .contains("version.workspace = true")
        );
    }

    #[test]
    fn sibling_crates_keep_their_own_version_line() {
        let dir = tempfile::tempdir().unwrap();
        virtual_workspace(dir.path(), "crates/demo-py");
        // 发到 crates.io 的独立 0.x 线，不能被包版本冲掉
        std::fs::create_dir_all(dir.path().join("crates/demo-core")).unwrap();
        std::fs::write(
            dir.path().join("crates/demo-core/Cargo.toml"),
            "[package]\nname = \"demo-core\"\nversion = \"0.2.0\"\n",
        )
        .unwrap();

        let mut build = MaturinBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("1.3.0")).unwrap();
        assert!(
            std::fs::read_to_string(dir.path().join("crates/demo-core/Cargo.toml"))
                .unwrap()
                .contains("version = \"0.2.0\"")
        );
    }

    #[test]
    fn builds_with_maturin_and_never_publishes() {
        let dir = tempfile::tempdir().unwrap();
        virtual_workspace(dir.path(), "crates/demo-py");
        let build = MaturinBuild::detect(dir.path()).unwrap().unwrap();

        let commands = build.cmd_build().unwrap();
        let maturin = commands
            .iter()
            .find(|c| c.starts_with("maturin build"))
            .expect("maturin build");
        assert!(maturin.contains("--release"), "{maturin}");
        assert!(maturin.contains("crates/demo-py/Cargo.toml"), "{maturin}");
        assert!(maturin.contains("dist/funbuild/root"), "{maturin}");
        // 不碰 uv.lock
        assert!(
            !commands.iter().any(|c| c.contains("uv.lock")),
            "{commands:?}"
        );
        assert!(
            !commands.iter().any(|c| c.starts_with("uv build")),
            "{commands:?}"
        );

        assert!(build.cmd_publish().unwrap().is_empty());
        // 安装校验仍走 uv 那套
        assert_eq!(
            build.cmd_install().unwrap(),
            vec!["uv pip install dist/funbuild/*/*.whl"]
        );
    }
}
