use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::builders::npm::NpmFrontendBuild;
use crate::builders::uv::UvBuild;
use crate::builders::{Builder, RepoContext};
use crate::error::Result;
use crate::shell;

/// 同一仓库里既是 uv 管理的 Python 包、又带可构建前端 package.json 的混合项目：
/// 一条命令串联两侧的构建 / 安装 / 发布。
///
/// 状态以 uv 那一侧为准（版本主源是根 pyproject），ctx 直接转发过去。
pub struct UvNpmHybridBuild {
    uv: UvBuild,
    npm: NpmFrontendBuild,
}

impl UvNpmHybridBuild {
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let Some(uv) = UvBuild::detect(repo_path)? else {
            return Ok(None);
        };
        let Some(npm) = NpmFrontendBuild::detect(repo_path)? else {
            return Ok(None);
        };
        log::info!(
            "detected hybrid repo: UV Python + npm/pnpm/yarn frontend, single build runs both"
        );
        Ok(Some(Self { uv, npm }))
    }
}

impl Builder for UvNpmHybridBuild {
    fn ctx(&self) -> &RepoContext {
        self.uv.ctx()
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        self.uv.ctx_mut()
    }

    fn kind(&self) -> &'static str {
        "UvNpmHybridBuild"
    }

    /// 只写 Python 侧：uv 的 write_version 会调版本同步，package.json 由它一并写到。
    fn write_version(&mut self) -> Result<()> {
        self.uv.write_version()
    }

    fn sync_latest_dependencies(&mut self) -> Result<()> {
        self.uv.sync_latest_dependencies()?;
        self.npm.sync_latest_dependencies()
    }

    fn cmd_build(&self) -> Result<Vec<String>> {
        let mut out = self.uv.cmd_build()?;
        out.extend(self.npm.cmd_build()?);
        Ok(out)
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        let mut out = self.uv.cmd_install()?;
        out.extend(self.npm.cmd_install()?);
        Ok(out)
    }

    fn cmd_publish(&self) -> Result<Vec<String>> {
        let mut out = self.uv.cmd_publish()?;
        out.extend(self.npm.cmd_publish()?);
        Ok(out)
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        let mut out: Vec<String> = Vec::new();
        for command in self
            .uv
            .cmd_delete()?
            .into_iter()
            .chain(self.npm.cmd_delete()?)
        {
            if !out.contains(&command) {
                out.push(command);
            }
        }
        Ok(out)
    }

    fn published_distributions(&self) -> Result<BTreeMap<String, PathBuf>> {
        self.uv.published_distributions()
    }

    fn install(&mut self) -> Result<()> {
        log::info!(
            "{} install (hybrid: uv wheel + frontend deps)",
            self.ctx().name
        );
        let mut commands = self.uv.cmd_build()?;
        commands.extend(self.uv.cmd_install()?);
        commands.extend(self.uv.cmd_delete()?);
        shell::run_checked(&commands, self.ctx().cwd())?;
        self.npm.install()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hybrid_repo(dir: &Path) {
        std::fs::write(
            dir.join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("package.json"),
            r#"{"name": "demo-web", "version": "1.0.0", "scripts": {"build": "vite build"}}"#,
        )
        .unwrap();
    }

    #[test]
    fn needs_both_sides() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nversion = \"1.0.0\"\n",
        )
        .unwrap();
        assert!(UvNpmHybridBuild::detect(dir.path()).unwrap().is_none());

        hybrid_repo(dir.path());
        assert!(UvNpmHybridBuild::detect(dir.path()).unwrap().is_some());
    }

    #[test]
    fn version_comes_from_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        hybrid_repo(dir.path());
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"2.5.0\"\n",
        )
        .unwrap();
        let build = UvNpmHybridBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("2.5.0"));
    }

    #[test]
    fn commands_chain_both_toolchains() {
        let dir = tempfile::tempdir().unwrap();
        hybrid_repo(dir.path());
        let build = UvNpmHybridBuild::detect(dir.path()).unwrap().unwrap();

        let commands = build.cmd_build().unwrap();
        assert!(
            commands.iter().any(|c| c.starts_with("uv build")),
            "{commands:?}"
        );
        assert!(
            commands.contains(&"npm run build".to_string()),
            "{commands:?}"
        );

        let publish = build.cmd_publish().unwrap();
        assert!(
            publish.iter().any(|c| c.starts_with("uv publish")),
            "{publish:?}"
        );
        assert!(publish.contains(&"npm publish".to_string()), "{publish:?}");
    }

    #[test]
    fn latest_dependencies_also_validate_frontend_config() {
        let dir = tempfile::tempdir().unwrap();
        hybrid_repo(dir.path());
        std::fs::write(
            dir.path().join("package.json"),
            r#"{"name":"demo-web","version":"1.0.0","packageManager":"pnpm@10.0.0","scripts":{"build":"x"},"funbuild":{"latest-packages":"invalid"}}"#,
        )
        .unwrap();
        let mut build = UvNpmHybridBuild::detect(dir.path()).unwrap().unwrap();
        assert!(build
            .sync_latest_dependencies()
            .unwrap_err()
            .to_string()
            .contains("funbuild.latest-packages"));
    }

    #[test]
    fn delete_commands_are_deduplicated() {
        let dir = tempfile::tempdir().unwrap();
        hybrid_repo(dir.path());
        let build = UvNpmHybridBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_delete().unwrap();
        let mut sorted = commands.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(commands.len(), sorted.len(), "重复项: {commands:?}");
        // 两侧都要删 dist，只该出现一次
        assert_eq!(commands.iter().filter(|c| *c == "rm -rf dist").count(), 1);
    }

    #[test]
    fn version_lands_in_both_manifests() {
        let dir = tempfile::tempdir().unwrap();
        hybrid_repo(dir.path());
        let mut build = UvNpmHybridBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("3.0.0")).unwrap();
        assert!(std::fs::read_to_string(dir.path().join("pyproject.toml"))
            .unwrap()
            .contains("version = \"3.0.0\""));
        assert!(std::fs::read_to_string(dir.path().join("package.json"))
            .unwrap()
            .contains("\"version\": \"3.0.0\""));
    }
}
