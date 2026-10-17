use std::path::Path;

use crate::builders::{Builder, RepoContext};
use crate::error::Result;
use crate::manifest::sync;
use crate::manifest::toml as toml_manifest;

/// 用 Poetry 管理版本与依赖的 Python 项目。
pub struct PoetryBuild {
    ctx: RepoContext,
}

impl PoetryBuild {
    /// 必须有 `[tool.poetry].version`。
    ///
    /// 只带 `[tool.ruff]` 之类其它 tool 段的 pyproject 不算 Poetry 项目，此处必须
    /// 返回 None 而不是报错，否则探测链会整个中断。
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let path = repo_path.join("pyproject.toml");
        if !path.exists() {
            return Ok(None);
        }
        let doc = match toml_manifest::load(&path) {
            Ok(doc) => doc,
            Err(error) => {
                log::warn!(
                    "skip poetry check, cannot parse {}: {error}",
                    path.display()
                );
                return Ok(None);
            }
        };
        let Some(version) = toml_manifest::dig_nonempty(&doc, &["tool", "poetry", "version"])
        else {
            return Ok(None);
        };
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        ctx.version = Some(version);
        Ok(Some(Self { ctx }))
    }
}

impl Builder for PoetryBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "PoetryBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        let path = self.ctx.repo_path.join("pyproject.toml");
        let mut doc = toml_manifest::load(&path)?;
        if let Some(table) = toml_manifest::dig_table_mut(&mut doc, &["tool", "poetry"]) {
            toml_manifest::set_value(table, "version", version.as_str());
        }
        toml_manifest::save(&doc, &path)?;
        sync::sync_all(&self.ctx.repo_path, &version)
    }

    fn cmd_build(&self) -> Result<Vec<String>> {
        Ok(vec!["poetry lock".to_string(), "poetry build".to_string()])
    }

    fn cmd_publish(&self) -> Result<Vec<String>> {
        Ok(vec!["poetry publish".to_string()])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_poetry_version() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.poetry]\nname = \"demo\"\nversion = \"1.2.3\"\n",
        )
        .unwrap();
        let build = PoetryBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.2.3"));
        assert_eq!(
            build.cmd_build().unwrap(),
            vec!["poetry lock", "poetry build"]
        );
        assert_eq!(build.cmd_publish().unwrap(), vec!["poetry publish"]);
    }

    #[test]
    fn other_tool_sections_are_not_poetry() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.ruff]\nline-length = 120\n",
        )
        .unwrap();
        assert!(PoetryBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn broken_manifest_skips_without_breaking_the_chain() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("pyproject.toml"), "[tool.poetry\nname = ").unwrap();
        assert!(PoetryBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn writes_version_keeping_comments() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.poetry]\n# 说明\nname = \"demo\"\nversion = \"1.2.3\"\n",
        )
        .unwrap();
        let mut build = PoetryBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let out = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(out.contains("version = \"1.2.4\""), "{out}");
        assert!(out.contains("# 说明"), "{out}");
    }
}
