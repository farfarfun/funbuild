use std::path::Path;

use crate::builders::{Builder, RepoContext};
use crate::error::{self, Result};
use crate::manifest::sync;
use crate::version::parse_version;

/// 以根目录纯文本 `VERSION` 文件为版本源的项目（如 shell 脚本仓库）。
///
/// 没有 pyproject.toml / package.json，无从构建或发布，但仍需要 upgrade / push /
/// tag 这套版本与 Git 流程。
pub struct VersionFileBuild {
    ctx: RepoContext,
}

impl VersionFileBuild {
    const FILE: &'static str = "VERSION";

    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let path = repo_path.join(Self::FILE);
        if !path.is_file() {
            return Ok(None);
        }
        let raw = match error::read_to_string(&path) {
            Ok(raw) => raw.trim().to_string(),
            Err(error) => {
                log::warn!("skip VERSION file {}: {error}", path.display());
                return Ok(None);
            }
        };
        if raw.is_empty() {
            return Ok(None);
        }
        if parse_version(&raw).is_err() {
            log::warn!("skip VERSION file, cannot parse content {raw:?}");
            return Ok(None);
        }
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        // 去掉可能的 v 前缀: tags() 会自行拼 v, 否则会得到 vv0.1.7
        ctx.version = Some(raw.strip_prefix('v').unwrap_or(&raw).to_string());
        Ok(Some(Self { ctx }))
    }
}

impl Builder for VersionFileBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "VersionFileBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        error::write(self.ctx.repo_path.join(Self::FILE), format!("{version}\n"))?;
        sync::sync_all(&self.ctx.repo_path, &version)
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_and_strips_v_prefix() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("VERSION"), "v1.2.3\n").unwrap();
        let build = VersionFileBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.2.3"));
    }

    #[test]
    fn skips_missing_empty_or_unparsable() {
        let dir = tempfile::tempdir().unwrap();
        assert!(VersionFileBuild::detect(dir.path()).unwrap().is_none());

        std::fs::write(dir.path().join("VERSION"), "   \n").unwrap();
        assert!(VersionFileBuild::detect(dir.path()).unwrap().is_none());

        std::fs::write(dir.path().join("VERSION"), "not-a-version\n").unwrap();
        assert!(VersionFileBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn writes_version_with_trailing_newline() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("VERSION"), "1.2.3\n").unwrap();
        let mut build = VersionFileBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join("VERSION")).unwrap(),
            "1.2.4\n"
        );
    }

    #[test]
    fn builds_nothing() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("VERSION"), "1.0.0").unwrap();
        let build = VersionFileBuild::detect(dir.path()).unwrap().unwrap();
        assert!(build.cmd_build().unwrap().is_empty());
        assert!(build.cmd_publish().unwrap().is_empty());
        assert!(build.cmd_install().unwrap().is_empty());
        assert!(build.cmd_delete().unwrap().is_empty());
    }
}
