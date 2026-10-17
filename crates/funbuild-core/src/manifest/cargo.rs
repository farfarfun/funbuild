//! Cargo.toml 里版本号的读与写，由 MaturinBuild 和 CargoBuild 共用。
//!
//! 不并进 `sync.rs`：那边是所有构建类型都会调的，把 Cargo.toml 加进去等于让
//! UVBuild 也去改兄弟 crate 的版本——而那些常有自己独立的版本线。

use std::path::Path;

use crate::error::{self, Result};
use crate::manifest::toml::{self as toml_manifest, version_line_re};

/// 只在这两个表下改 version：`[dependencies]` 里的是依赖约束，不跟包版本走。
const VERSION_SECTIONS: [&str; 2] = ["[workspace.package]", "[package]"];

/// 读版本的优先级。`[package].version` 在前是 cargo 自己的语义——它才是
/// `cargo publish` 上传的那个版本。
pub fn read_version(path: impl AsRef<Path>) -> Result<Option<String>> {
    let doc = toml_manifest::load(path)?;
    // `version.workspace = true` 取出来是表而不是字符串，这里自然落到下一个 key
    Ok(toml_manifest::dig_nonempty(&doc, &["package", "version"])
        .or_else(|| toml_manifest::dig_nonempty(&doc, &["workspace", "package", "version"])))
}

/// 把 `[workspace.package]` / `[package]` 下的 version 改成 `version`，改过返回 true。
///
/// 手写行替换而不用 toml_edit：按 tag 发布的 workflow 普遍用
/// `grep -m1 '^version = ' Cargo.toml` 取版本去和 tag 比对，版本必须留在行首那个形状上。
pub fn sync_version(path: impl AsRef<Path>, version: &str) -> Result<bool> {
    let path = path.as_ref();
    let raw = error::read_to_string(path)?;
    let re = version_line_re();

    let mut section: Option<&str> = None;
    let mut changed = 0usize;
    let mut out = String::with_capacity(raw.len());

    for line in raw.split_inclusive('\n') {
        let newline = line.ends_with('\n');
        let body = line.strip_suffix('\n').unwrap_or(line);
        let stripped = body.trim();

        if stripped.starts_with('[') && stripped.ends_with(']') {
            section = Some(stripped);
            out.push_str(line);
            continue;
        }
        if !section.is_some_and(|s| VERSION_SECTIONS.contains(&s)) {
            out.push_str(line);
            continue;
        }
        match re.captures(body) {
            Some(caps) => {
                changed += 1;
                out.push_str(&caps["prefix"]);
                out.push_str(version);
                out.push_str(&caps["suffix"]);
                if newline {
                    out.push('\n');
                }
            }
            None => out.push_str(line),
        }
    }

    if changed == 0 {
        // 不警告：单个清单没有字面量 version 是正常的（`version.workspace = true`），
        // 该不该报由调用方判断。
        return Ok(false);
    }
    error::write(path, out)?;
    log::info!("同步版本号到 {}: {version}", path.display());
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, body: &str) -> std::path::PathBuf {
        let path = dir.join("Cargo.toml");
        std::fs::write(&path, body).unwrap();
        path
    }

    #[test]
    fn reads_package_version_first() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(
            dir.path(),
            "[workspace.package]\nversion = \"9.9.9\"\n\n[package]\nversion = \"1.2.3\"\n",
        );
        assert_eq!(read_version(&path).unwrap().as_deref(), Some("1.2.3"));
    }

    #[test]
    fn falls_back_to_workspace_package() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(
            dir.path(),
            "[workspace.package]\nversion = \"1.2.0\"\n\n[package]\nversion.workspace = true\n",
        );
        assert_eq!(read_version(&path).unwrap().as_deref(), Some("1.2.0"));
    }

    #[test]
    fn virtual_workspace_without_literal_version() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(dir.path(), "[workspace]\nmembers = [\"a\"]\n");
        assert_eq!(read_version(&path).unwrap(), None);
    }

    #[test]
    fn syncs_only_package_sections() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(
            dir.path(),
            "[workspace.package]\nversion = \"1.0.0\"\n\n[workspace.dependencies]\nfoo = { path = \"x\", version = \"0.2.0\" }\n\n[dependencies]\nversion = \"should-not-change\"\n",
        );
        // 行内的依赖 version 不该被动；[dependencies] 下独立的 version 行也不该动
        assert!(sync_version(&path, "1.0.1").unwrap());
        let out = std::fs::read_to_string(&path).unwrap();
        assert!(
            out.contains("[workspace.package]\nversion = \"1.0.1\""),
            "{out}"
        );
        assert!(out.contains("version = \"0.2.0\" }"), "{out}");
        assert!(out.contains("version = \"should-not-change\""), "{out}");
    }

    #[test]
    fn keeps_comments_and_layout() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(
            dir.path(),
            "# 为什么不开 gzip 的那一大段说明\n[package]\nname = \"demo\"\nversion = \"1.0.0\"\nedition = \"2021\"\n",
        );
        sync_version(&path, "1.0.1").unwrap();
        let out = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            out,
            "# 为什么不开 gzip 的那一大段说明\n[package]\nname = \"demo\"\nversion = \"1.0.1\"\nedition = \"2021\"\n"
        );
    }

    #[test]
    fn version_stays_at_line_start_for_grep() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(dir.path(), "[package]\nversion = \"1.0.0\"\n");
        sync_version(&path, "2.0.0").unwrap();
        let out = std::fs::read_to_string(&path).unwrap();
        let first = out.lines().find(|l| l.starts_with("version = ")).unwrap();
        assert_eq!(first, "version = \"2.0.0\"");
    }

    #[test]
    fn reports_nothing_to_change() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(dir.path(), "[package]\nversion.workspace = true\n");
        assert!(!sync_version(&path, "1.0.1").unwrap());
    }
}
