//! 把同一个版本号写进仓库里所有带 version 的清单。
//!
//! 解析失败一律中止发布：静默少同步一个清单，结果是线上版本与仓库清单不一致。

use std::path::{Path, PathBuf};

use crate::error::{self, Error, Result};
use crate::manifest::{json, pubspec, toml as toml_manifest};

/// 多包仓库放子包的两个目录名。
const SUB_GROUPS: [&str; 2] = ["extbuild", "exts"];

/// 根清单 + `extbuild/*`、`exts/*` 下的同名清单，只返回真实存在的。
pub fn manifest_paths(root: &Path, filename: &str) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let at_root = root.join(filename);
    if at_root.is_file() {
        paths.push(at_root);
    }
    for group in SUB_GROUPS {
        let dir = root.join(group);
        if !dir.is_dir() {
            continue;
        }
        let mut subs: Vec<PathBuf> = std::fs::read_dir(&dir)
            .into_iter()
            .flatten()
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.is_dir())
            .collect();
        subs.sort();
        for sub in subs {
            let candidate = sub.join(filename);
            if candidate.is_file() {
                paths.push(candidate);
            }
        }
    }
    paths
}

/// 根 pyproject.toml 的 `[project].version`，整仓版本主源。
///
/// 只读探测，拿不到就让调用方回退到自己已解析的值——但不能连日志都不留。
pub fn root_pyproject_version(root: &Path) -> Option<String> {
    let path = root.join("pyproject.toml");
    if !path.is_file() {
        return None;
    }
    match toml_manifest::load(&path) {
        Ok(doc) => toml_manifest::dig_nonempty(&doc, &["project", "version"]),
        Err(error) => {
            log::warn!(
                "无法从 {} 读取主版本号，回退到调用方自己解析的值: {error}",
                path.display()
            );
            None
        }
    }
}

fn pyproject_participates(path: &Path) -> Result<bool> {
    let doc = toml_manifest::load(path).map_err(|e| {
        Error::version_sync(format!(
            "版本清单无法解析, 拒绝带着不一致的版本继续发布: {}: {e}",
            path.display()
        ))
    })?;
    Ok(toml_manifest::dig(&doc, &["project", "version"]).is_some()
        || toml_manifest::dig(&doc, &["tool", "poetry", "version"]).is_some())
}

fn sync_pyproject(path: &Path, version: &str) -> Result<()> {
    let mut doc = toml_manifest::load(path)?;
    let mut changed = false;
    if toml_manifest::dig(&doc, &["project", "version"]).is_some() {
        if let Some(table) = toml_manifest::dig_table_mut(&mut doc, &["project"]) {
            toml_manifest::set_value(table, "version", version);
            changed = true;
        }
    }
    if toml_manifest::dig(&doc, &["tool", "poetry", "version"]).is_some() {
        if let Some(table) = toml_manifest::dig_table_mut(&mut doc, &["tool", "poetry"]) {
            toml_manifest::set_value(table, "version", version);
            changed = true;
        }
    }
    if !changed {
        return Ok(());
    }
    toml_manifest::save(&doc, path)
}

fn sync_package_json(path: &Path, version: &str) -> Result<()> {
    let mut pkg = json::load_object(path)?;
    pkg.insert(
        "version".into(),
        serde_json::Value::String(version.to_string()),
    );
    json::save(&pkg, path)
}

/// 只同步 major.minor.patch，`+buildNumber` 后缀保持不变——那是 FlutterBuild
/// 自己在 upgrade 时才递增的字段。
fn sync_pubspec(path: &Path, version: &str) -> Result<()> {
    let raw = error::read_to_string(path)?;
    let (updated, count) =
        pubspec::replace_version_line(&raw, |inner| match inner.split_once('+') {
            Some((_, build)) => format!("{version}+{build}"),
            None => version.to_string(),
        });
    if count == 0 {
        return Ok(());
    }
    error::write(path, updated)
}

/// 把 `version` 写进仓库内所有带 version 的 pyproject.toml、package.json 与 pubspec.yaml。
pub fn sync_all(root: &Path, version: &str) -> Result<()> {
    let version = version.trim();

    for path in manifest_paths(root, "package.json") {
        let pkg = json::load_object(&path).map_err(|e| {
            Error::version_sync(format!(
                "版本清单无法解析, 拒绝带着不一致的版本继续发布: {}: {e}",
                path.display()
            ))
        })?;
        if json::str_field(&pkg, "version").is_none() {
            continue;
        }
        sync_package_json(&path, version).map_err(|e| {
            Error::version_sync(format!("同步版本清单失败: {}: {e}", path.display()))
        })?;
    }

    for path in manifest_paths(root, "pyproject.toml") {
        if !pyproject_participates(&path)? {
            continue;
        }
        sync_pyproject(&path, version).map_err(|e| {
            Error::version_sync(format!("同步版本清单失败: {}: {e}", path.display()))
        })?;
    }

    for path in manifest_paths(root, "pubspec.yaml") {
        let raw = error::read_to_string(&path).map_err(|e| {
            Error::version_sync(format!(
                "版本清单无法读取, 拒绝带着不一致的版本继续发布: {}: {e}",
                path.display()
            ))
        })?;
        if !pubspec::has_version_line(&raw) {
            continue;
        }
        sync_pubspec(&path, version).map_err(|e| {
            Error::version_sync(format!("同步版本清单失败: {}: {e}", path.display()))
        })?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(path: &Path, body: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    #[test]
    fn syncs_every_manifest_kind_including_subpackages() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(
            &root.join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        write(
            &root.join("package.json"),
            "{\n  \"name\": \"demo\",\n  \"version\": \"1.0.0\"\n}\n",
        );
        write(&root.join("pubspec.yaml"), "name: demo\nversion: 1.0.0+7\n");
        write(
            &root.join("extbuild/sub/pyproject.toml"),
            "[project]\nname = \"sub\"\nversion = \"0.1.0\"\n",
        );
        write(
            &root.join("exts/other/package.json"),
            "{\n  \"version\": \"0.1.0\"\n}\n",
        );

        sync_all(root, "2.3.4").unwrap();

        assert!(std::fs::read_to_string(root.join("pyproject.toml"))
            .unwrap()
            .contains("version = \"2.3.4\""));
        assert!(std::fs::read_to_string(root.join("package.json"))
            .unwrap()
            .contains("\"version\": \"2.3.4\""));
        // pubspec 的构建号不动
        assert_eq!(
            std::fs::read_to_string(root.join("pubspec.yaml")).unwrap(),
            "name: demo\nversion: 2.3.4+7\n"
        );
        assert!(
            std::fs::read_to_string(root.join("extbuild/sub/pyproject.toml"))
                .unwrap()
                .contains("version = \"2.3.4\"")
        );
        assert!(
            std::fs::read_to_string(root.join("exts/other/package.json"))
                .unwrap()
                .contains("\"version\": \"2.3.4\"")
        );
    }

    #[test]
    fn syncs_poetry_version_too() {
        let dir = tempfile::tempdir().unwrap();
        write(
            &dir.path().join("pyproject.toml"),
            "[tool.poetry]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        sync_all(dir.path(), "1.0.1").unwrap();
        assert!(std::fs::read_to_string(dir.path().join("pyproject.toml"))
            .unwrap()
            .contains("version = \"1.0.1\""));
    }

    #[test]
    fn skips_manifests_without_version() {
        let dir = tempfile::tempdir().unwrap();
        // 只有 [tool.ruff] 的 pyproject 不参与同步，也不该报错
        write(
            &dir.path().join("pyproject.toml"),
            "[tool.ruff]\nline-length = 120\n",
        );
        sync_all(dir.path(), "1.0.1").unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap(),
            "[tool.ruff]\nline-length = 120\n"
        );
    }

    #[test]
    fn refuses_to_continue_on_broken_manifest() {
        let dir = tempfile::tempdir().unwrap();
        write(
            &dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\n",
        );
        let error = sync_all(dir.path(), "1.0.1").unwrap_err();
        assert!(matches!(error, Error::ManifestVersionSync(_)), "{error}");
        assert!(
            error.to_string().contains("拒绝带着不一致的版本继续发布"),
            "{error}"
        );
    }

    #[test]
    fn root_version_is_the_source_of_truth() {
        let dir = tempfile::tempdir().unwrap();
        write(
            &dir.path().join("pyproject.toml"),
            "[project]\nversion = \"3.2.1\"\n",
        );
        assert_eq!(root_pyproject_version(dir.path()).as_deref(), Some("3.2.1"));
    }

    #[test]
    fn root_version_absent_without_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(root_pyproject_version(dir.path()), None);
    }

    #[test]
    fn keeps_comments_in_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        write(
            &dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n\n[tool.funbuild]\n# 这段注释必须留住\nlatest-packages = [\"farlog\"]\n",
        );
        sync_all(dir.path(), "1.0.1").unwrap();
        let out = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(out.contains("# 这段注释必须留住"), "{out}");
    }
}
