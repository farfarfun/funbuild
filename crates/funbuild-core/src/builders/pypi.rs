use std::path::Path;

use crate::builders::{Builder, RepoContext};
use crate::error::{self, Result};
use crate::manifest::sync;

/// 早期遗留的 PyPI 发布脚本项目：版本源是 `script/__version__.md`。
pub struct PypiBuild {
    ctx: RepoContext,
}

impl PypiBuild {
    const FILE: &'static str = "script/__version__.md";

    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let path = repo_path.join(Self::FILE);
        if !path.exists() {
            return Ok(None);
        }
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        ctx.version = Some(error::read_to_string(&path)?.trim().to_string());
        Ok(Some(Self { ctx }))
    }
}

impl Builder for PypiBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "PypiBuild"
    }

    /// `script/__version__.md` 是这类仓库的版本源头，不能删除，只能与其它清单一起同步。
    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        error::write(self.ctx.repo_path.join(Self::FILE), &version)?;
        sync::sync_all(&self.ctx.repo_path, &version)
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(vec!["pip install dist/*.whl".to_string()])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(dir: &Path, version: &str) {
        std::fs::create_dir_all(dir.join("script")).unwrap();
        std::fs::write(dir.join("script/__version__.md"), version).unwrap();
    }

    #[test]
    fn detects_version_from_file() {
        let dir = tempfile::tempdir().unwrap();
        fixture(dir.path(), "1.2.3\n");
        let build = PypiBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.2.3"));
    }

    #[test]
    fn absent_without_the_marker_file() {
        let dir = tempfile::tempdir().unwrap();
        assert!(PypiBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn writes_version_without_trailing_newline() {
        let dir = tempfile::tempdir().unwrap();
        fixture(dir.path(), "1.2.3");
        let mut build = PypiBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join("script/__version__.md")).unwrap(),
            "1.2.4"
        );
    }

    #[test]
    fn install_has_no_force_reinstall() {
        let dir = tempfile::tempdir().unwrap();
        fixture(dir.path(), "1.0.0");
        let build = PypiBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_install().unwrap(), vec!["pip install dist/*.whl"]);
    }
}
