use std::path::PathBuf;

use crate::builders::{Builder, RepoContext};
use crate::error::Result;

/// 兜底构建类型：所有命令均为空操作。永远匹配，只在探测链走完后才用。
pub struct EmptyBuild {
    ctx: RepoContext,
}

impl EmptyBuild {
    pub fn fallback(repo_path: PathBuf) -> Self {
        log::warn!(
            "未识别到版本清单 (pyproject.toml / package.json / Cargo.toml / VERSION / \
             script/__version__.md), 回退到 EmptyBuild: upgrade、tag、build 将不会有任何效果"
        );
        Self {
            ctx: RepoContext::new(repo_path),
        }
    }
}

impl Builder for EmptyBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "EmptyBuild"
    }

    /// 没有版本清单可写。
    fn write_version(&mut self) -> Result<()> {
        Ok(())
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }
}
