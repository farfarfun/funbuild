use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::builders::{Builder, RepoContext};
use crate::error::Result;
use crate::manifest::{json, pubspec, toml as toml_manifest};
use crate::util::normalize_package_name;
use crate::version::parse_version;
use crate::{commit, git, shell};

/// `<product>-dev` 编排仓库：`apps/` 下每个 app 是独立仓库的 git submodule。
///
/// 仓库自身没有可构建产物，`scripts/funbuild.toml` 里的 version 是分发给所有 app 的
/// 共享版本。build() 因此完全不走命令管线，而是逐个 app 转发
/// `funbuild build --version <共享版本号>`，最后统一 push、打 tag 一次。
pub struct SubmoduleWorkspaceBuild {
    ctx: RepoContext,
}

impl SubmoduleWorkspaceBuild {
    const CONFIG: &'static str = "scripts/funbuild.toml";

    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        if !repo_path.join("apps").is_dir() {
            return Ok(None);
        }
        let config = repo_path.join(Self::CONFIG);
        if !config.is_file() {
            return Ok(None);
        }
        let doc = match toml_manifest::load(&config) {
            Ok(doc) => doc,
            Err(error) => {
                log::warn!("skip {}: {error}", config.display());
                return Ok(None);
            }
        };

        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        match doc.get("version") {
            None => ctx.version = None,
            Some(item) => {
                let Some(raw) = item.as_str() else {
                    log::warn!(
                        "skip {}, version must be a string, got {item:?}",
                        config.display()
                    );
                    return Ok(None);
                };
                let raw = raw.trim();
                if raw.is_empty() {
                    ctx.version = None;
                } else {
                    if parse_version(raw).is_err() {
                        log::warn!("skip {}, cannot parse version {raw:?}", config.display());
                        return Ok(None);
                    }
                    // 去掉可能的 v 前缀: tags() 会自行拼 v
                    ctx.version = Some(raw.strip_prefix('v').unwrap_or(raw).to_string());
                }
            }
        }
        Ok(Some(Self { ctx }))
    }

    fn config_path(&self) -> PathBuf {
        self.ctx.repo_path.join(Self::CONFIG)
    }

    /// `packages` 里配置的包名：apps/ 下任何 submodule 若依赖这些包，build 前会被
    /// 升级到最新版，而不是继续用各自清单里可能早已过期的版本约束。
    fn pinned_packages(&self) -> Vec<String> {
        match toml_manifest::load(self.config_path()) {
            Ok(doc) => toml_manifest::dig_string_list(&doc, &["packages"]),
            Err(error) => {
                log::warn!("skip {}: {error}", self.config_path().display());
                Vec::new()
            }
        }
    }

    /// 某 app `[project].dependencies` 引用的包名（已归一化）。
    fn dependency_names_uv(path: &Path) -> Vec<String> {
        match toml_manifest::load(path) {
            Ok(doc) => toml_manifest::dig_string_list(&doc, &["project", "dependencies"])
                .iter()
                .filter_map(|dep| {
                    let name: String = dep
                        .chars()
                        .take_while(|c| {
                            c.is_ascii_alphanumeric() || *c == '_' || *c == '.' || *c == '-'
                        })
                        .collect();
                    (!name.is_empty()).then(|| normalize_package_name(&name))
                })
                .collect(),
            Err(error) => {
                log::warn!("skip reading dependencies from {}: {error}", path.display());
                Vec::new()
            }
        }
    }

    fn dependency_names_npm(path: &Path) -> Vec<String> {
        let Ok(pkg) = json::load_object(path).inspect_err(|error| {
            log::warn!("skip reading dependencies from {}: {error}", path.display());
        }) else {
            return Vec::new();
        };
        let mut names = Vec::new();
        for field in ["dependencies", "devDependencies"] {
            if let Some(deps) = pkg.get(field).and_then(|d| d.as_object()) {
                names.extend(deps.keys().map(|name| normalize_package_name(name)));
            }
        }
        names
    }

    fn dependency_names_flutter(path: &Path) -> Vec<String> {
        match pubspec::load(path) {
            Ok(data) => pubspec::dependency_names(&data)
                .iter()
                .map(|name| normalize_package_name(name))
                .collect(),
            Err(error) => {
                log::warn!("skip reading dependencies from {}: {error}", path.display());
                Vec::new()
            }
        }
    }

    fn detect_npm_pm(path: &Path) -> &'static str {
        if path.join("pnpm-lock.yaml").exists() {
            return "pnpm";
        }
        if path.join("yarn.lock").exists() {
            return "yarn";
        }
        "npm"
    }

    /// 命中的包名，按归一化名排序。
    fn matched(pinned: &BTreeMap<String, String>, declared: &[String]) -> Vec<String> {
        let mut hits: Vec<String> = pinned
            .iter()
            .filter(|(normalized, _)| declared.contains(normalized))
            .map(|(_, original)| original.clone())
            .collect();
        hits.sort();
        hits
    }

    /// `uv add <pkg>@latest` 不是合法语法（`@` 后会被当成 PEP 508 直接引用）。正确
    /// 做法是 remove 再 add：不带版本号的 `uv add` 会按该 app 自己配置的 index 解析
    /// 最新版并重写依赖声明，而纯 add 对已存在的依赖是空操作。
    fn upgrade_pinned_uv(
        path: &Path,
        manifest: &Path,
        pinned: &BTreeMap<String, String>,
    ) -> Result<()> {
        let declared = Self::dependency_names_uv(manifest);
        for pkg in Self::matched(pinned, &declared) {
            log::info!(
                "upgrade pinned dependency to latest (uv): {pkg} ({})",
                path.display()
            );
            shell::run_checked(
                &[
                    shell::join(["uv", "remove", &pkg]),
                    shell::join(["uv", "add", &pkg]),
                ],
                Some(path),
            )?;
        }
        Ok(())
    }

    fn upgrade_pinned_npm(
        path: &Path,
        manifest: &Path,
        pinned: &BTreeMap<String, String>,
    ) -> Result<()> {
        let declared = Self::dependency_names_npm(manifest);
        let hits = Self::matched(pinned, &declared);
        if hits.is_empty() {
            return Ok(());
        }
        let pm = Self::detect_npm_pm(path);
        for pkg in hits {
            log::info!(
                "upgrade pinned dependency to latest ({pm}): {pkg} ({})",
                path.display()
            );
            let latest = format!("{pkg}@latest");
            let command = match pm {
                "pnpm" => shell::join(["pnpm", "add", &latest]),
                "yarn" => shell::join(["yarn", "add", &latest]),
                _ => shell::join(["npm", "install", &latest, "--save"]),
            };
            shell::run_checked(&[command], Some(path))?;
        }
        Ok(())
    }

    /// 委托给 `dart pub add`：包已在依赖里时，pub 会把约束重写为解析到的最新可用版本。
    fn upgrade_pinned_flutter(
        path: &Path,
        manifest: &Path,
        pinned: &BTreeMap<String, String>,
    ) -> Result<()> {
        let declared = Self::dependency_names_flutter(manifest);
        for pkg in Self::matched(pinned, &declared) {
            log::info!(
                "upgrade pinned dependency to latest (pub): {pkg} ({})",
                path.display()
            );
            shell::run_checked(&[shell::join(["dart", "pub", "add", &pkg])], Some(path))?;
        }
        Ok(())
    }

    /// 按 app 目录下存在的清单分派到对应生态；一个 app 里有多种清单时都会处理。
    fn upgrade_pinned_packages(&self, path: &Path) -> Result<()> {
        let pinned = self.pinned_packages();
        if pinned.is_empty() {
            return Ok(());
        }
        let pinned: BTreeMap<String, String> = pinned
            .into_iter()
            .map(|pkg| (normalize_package_name(&pkg), pkg))
            .collect();

        let pyproject = path.join("pyproject.toml");
        if pyproject.is_file() {
            Self::upgrade_pinned_uv(path, &pyproject, &pinned)?;
        }
        let package_json = path.join("package.json");
        if package_json.is_file() {
            Self::upgrade_pinned_npm(path, &package_json, &pinned)?;
        }
        let pubspec_path = path.join("pubspec.yaml");
        if pubspec_path.is_file() {
            Self::upgrade_pinned_flutter(path, &pubspec_path, &pinned)?;
        }
        Ok(())
    }

    /// 结构性判断，不看名字：自己也有 apps/ + scripts/setup.sh 的 submodule 是嵌套
    /// 工作区，递归发布是它自己的事。
    fn is_nested_workspace(path: &Path) -> bool {
        path.join("apps").is_dir() && path.join("scripts/setup.sh").is_file()
    }

    /// submodule 常年处于 detached HEAD，此时子进程里的 git pull/push 都没有可用的
    /// 上游分支。只在确实 detached 时才切换，已经在分支上则不动。
    fn switch_to_tracked_branch(path: &Path) -> Result<()> {
        let dir = path.display().to_string();
        let current = shell::capture_stdout(
            &shell::join(["git", "-C", &dir, "symbolic-ref", "-q", "HEAD"]),
            None,
            None,
        );
        if current.trim_start().starts_with("refs/heads/") {
            return Ok(());
        }
        let remote_head = shell::capture_stdout(
            &shell::join([
                "git",
                "-C",
                &dir,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
            ]),
            None,
            None,
        );
        let remote_head = remote_head.trim();
        let branch = if remote_head.starts_with("refs/remotes/") {
            remote_head.rsplit('/').next().unwrap_or("master")
        } else {
            "master"
        };
        shell::run_checked(&[shell::join(["git", "-C", &dir, "switch", branch])], None)
    }
}

impl Builder for SubmoduleWorkspaceBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "SubmoduleWorkspaceBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        let path = self.config_path();
        let mut doc = toml_manifest::load(&path)?;
        toml_manifest::set_value(doc.as_table_mut(), "version", version.as_str());
        toml_manifest::save(&doc, &path)
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    /// 递增共享版本号，对每个 app submodule 转发同一版本号的 build，最后统一
    /// push、打 tag 一次，使所有 submodule 指针更新落成一个提交。
    fn build(&mut self, message: Option<&str>, target_version: Option<&str>) -> Result<()> {
        // 非法信息必须在任何 app 发布之前拦住
        commit::warn_if_invalid(message);
        log::info!("{} build (submodule workspace)", self.ctx.name);
        self.pull()?;
        self.upgrade(target_version)?;

        let version = self.ctx.version.clone().unwrap_or_default();
        for path in git::submodule_paths(&self.ctx.repo_path) {
            if Self::is_nested_workspace(&path) {
                log::info!(
                    "skip nested workspace, does not join the shared version: {}",
                    path.display()
                );
                continue;
            }
            log::info!("build {} with shared version {version}", path.display());
            Self::switch_to_tracked_branch(&path)?;
            shell::run_checked(
                &[shell::join([
                    "git",
                    "-C",
                    &path.display().to_string(),
                    "pull",
                ])],
                None,
            )?;
            self.upgrade_pinned_packages(&path)?;

            let mut cmd = vec![
                super::self_command(),
                "build".to_string(),
                "--version".to_string(),
                version.clone(),
            ];
            if let Some(message) = message {
                cmd.push(message.to_string());
            }
            shell::run_checked(&[shell::join(&cmd)], Some(&path))?;
        }
        self.push(message, 20)?;
        self.tags()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn workspace(dir: &Path, config: &str) {
        std::fs::create_dir_all(dir.join("apps")).unwrap();
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts/funbuild.toml"), config).unwrap();
    }

    #[test]
    fn needs_both_apps_dir_and_config() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("apps")).unwrap();
        assert!(SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .is_none());

        workspace(dir.path(), "version = \"1.0.7\"\n");
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.0.7"));
    }

    #[test]
    fn config_without_version_is_still_claimed() {
        let dir = tempfile::tempdir().unwrap();
        workspace(dir.path(), "packages = [\"funflix\"]\n");
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        assert_eq!(build.ctx().version, None);
    }

    #[test]
    fn strips_v_prefix_and_rejects_garbage() {
        let dir = tempfile::tempdir().unwrap();
        workspace(dir.path(), "version = \"v2.0.91\"\n");
        assert_eq!(
            SubmoduleWorkspaceBuild::detect(dir.path())
                .unwrap()
                .unwrap()
                .ctx()
                .version
                .as_deref(),
            Some("2.0.91")
        );

        workspace(dir.path(), "version = \"not-a-version\"\n");
        assert!(SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .is_none());

        workspace(dir.path(), "version = 123\n");
        assert!(SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .is_none());
    }

    #[test]
    fn upgrade_writes_shared_version_keeping_packages() {
        let dir = tempfile::tempdir().unwrap();
        workspace(
            dir.path(),
            "version = \"1.0.0\"\npackages = [\"funflix\"]\n",
        );
        let mut build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("scripts/funbuild.toml")).unwrap();
        assert!(body.contains("version = \"1.0.1\""), "{body}");
        assert!(body.contains("packages = [\"funflix\"]"), "{body}");
    }

    #[test]
    fn no_build_commands_of_its_own() {
        let dir = tempfile::tempdir().unwrap();
        workspace(dir.path(), "version = \"1.0.0\"\n");
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        assert!(build.cmd_build().unwrap().is_empty());
        assert!(build.cmd_install().unwrap().is_empty());
        assert!(build.cmd_publish().unwrap().is_empty());
        assert!(build.cmd_delete().unwrap().is_empty());
    }

    #[test]
    fn reads_pinned_packages() {
        let dir = tempfile::tempdir().unwrap();
        workspace(
            dir.path(),
            "version = \"1.0.0\"\npackages = [\"funflix\", \"funworker\"]\n",
        );
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        assert_eq!(build.pinned_packages(), vec!["funflix", "funworker"]);
    }

    #[test]
    fn detects_nested_workspace_structurally() {
        let dir = tempfile::tempdir().unwrap();
        let app = dir.path().join("apps/inner");
        std::fs::create_dir_all(app.join("apps")).unwrap();
        std::fs::create_dir_all(app.join("scripts")).unwrap();
        assert!(!SubmoduleWorkspaceBuild::is_nested_workspace(&app));
        std::fs::write(app.join("scripts/setup.sh"), "#!/bin/sh\n").unwrap();
        assert!(SubmoduleWorkspaceBuild::is_nested_workspace(&app));
    }

    #[test]
    fn matches_dependency_names_across_ecosystems() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\ndependencies = [\"Funflix_API>=1.0\", \"requests\"]\n",
        )
        .unwrap();
        assert_eq!(
            SubmoduleWorkspaceBuild::dependency_names_uv(&dir.path().join("pyproject.toml")),
            vec!["funflix-api", "requests"]
        );

        std::fs::write(
            dir.path().join("package.json"),
            r#"{"dependencies":{"funflix":"^1.0.0"},"devDependencies":{"vite":"^5"}}"#,
        )
        .unwrap();
        assert_eq!(
            SubmoduleWorkspaceBuild::dependency_names_npm(&dir.path().join("package.json")),
            vec!["funflix", "vite"]
        );

        std::fs::write(
            dir.path().join("pubspec.yaml"),
            "dependencies:\n  flutter:\n    sdk: flutter\n  funtrack: ^1.0.0\n",
        )
        .unwrap();
        // flutter SDK 自带依赖被排除
        assert_eq!(
            SubmoduleWorkspaceBuild::dependency_names_flutter(&dir.path().join("pubspec.yaml")),
            vec!["funtrack"]
        );
    }

    #[test]
    fn pinned_upgrade_is_a_noop_without_matches() {
        let dir = tempfile::tempdir().unwrap();
        workspace(
            dir.path(),
            "version = \"1.0.0\"\npackages = [\"funflix\"]\n",
        );
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();

        let app = dir.path().join("apps/one");
        std::fs::create_dir_all(&app).unwrap();
        std::fs::write(
            app.join("pyproject.toml"),
            "[project]\ndependencies = [\"requests\"]\n",
        )
        .unwrap();
        // 没命中就不该跑任何 uv 命令（真跑了会因为不是 uv 项目而失败）
        build.upgrade_pinned_packages(&app).unwrap();
    }

    #[test]
    fn no_pinned_config_skips_entirely() {
        let dir = tempfile::tempdir().unwrap();
        workspace(dir.path(), "version = \"1.0.0\"\n");
        let build = SubmoduleWorkspaceBuild::detect(dir.path())
            .unwrap()
            .unwrap();
        let app = dir.path().join("apps/one");
        std::fs::create_dir_all(&app).unwrap();
        std::fs::write(
            app.join("pyproject.toml"),
            "[project]\ndependencies = [\"funflix\"]\n",
        )
        .unwrap();
        build.upgrade_pinned_packages(&app).unwrap();
    }
}
