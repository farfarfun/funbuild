use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::builders::{Builder, RepoContext};
use crate::error::{Error, Result};
use crate::manifest::{json, sync};
use crate::shell;
use crate::util::safe_clean_dir;

/// 基于 package.json 的前端项目（npm / pnpm / yarn），支持 extbuild/exts 子目录多包。
pub struct NpmFrontendBuild {
    ctx: RepoContext,
    pub(crate) package_json_paths: Vec<PathBuf>,
    /// 首个 package.json 里的 funbuild 配置，只用于 cmd_delete 的 cleanDirs。
    primary_cfg: Map<String, Value>,
}

fn funbuild_cfg(pkg: &Map<String, Value>) -> Map<String, Value> {
    match pkg.get("funbuild") {
        Some(Value::Object(map)) => map.clone(),
        Some(Value::Bool(true)) => [("enabled".to_string(), Value::Bool(true))]
            .into_iter()
            .collect(),
        _ => Map::new(),
    }
}

fn cfg_str(cfg: &Map<String, Value>, key: &str) -> Option<String> {
    let value = cfg.get(key)?.as_str()?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

/// 带 bin 字段的包是要 `npm install -g` 的 CLI，不是纯前端应用。
fn package_has_bin(pkg: &Map<String, Value>) -> bool {
    match pkg.get("bin") {
        Some(Value::String(value)) => !value.trim().is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
        _ => false,
    }
}

fn package_qualifies(pkg: &Map<String, Value>) -> bool {
    if json::str_field(pkg, "version").is_none() {
        return false;
    }
    let cfg = funbuild_cfg(pkg);
    let has_build_script = pkg
        .get("scripts")
        .and_then(|s| s.as_object())
        .is_some_and(|scripts| scripts.contains_key("build"));
    has_build_script || cfg_str(&cfg, "build").is_some()
}

/// 推断包管理器：显式配置 > packageManager 字段 > lockfile。
fn detect_pm(pkg_dir: &Path, cfg: &Map<String, Value>, pkg: &Map<String, Value>) -> String {
    if let Some(pm) = cfg
        .get("packageManager")
        .or_else(|| pkg.get("packageManager"))
        .and_then(|v| v.as_str())
    {
        let base = pm.split('@').next().unwrap_or("").trim().to_lowercase();
        if ["npm", "pnpm", "yarn"].contains(&base.as_str()) {
            return base;
        }
    }
    for (lockfile, pm) in [
        ("pnpm-lock.yaml", "pnpm"),
        ("package-lock.json", "npm"),
        ("yarn.lock", "yarn"),
    ] {
        if pkg_dir.join(lockfile).exists() {
            return pm.to_string();
        }
    }
    "npm".to_string()
}

/// 用子 shell 执行，避免 ` && ` 串联时 cd 残留导致后续相对路径错位。
fn in_dir_shell(pkg_dir: &str, inner: &str) -> String {
    if pkg_dir == "." || pkg_dir.is_empty() {
        return inner.to_string();
    }
    format!("(cd {} && {inner})", shell::quote(pkg_dir))
}

impl NpmFrontendBuild {
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let paths = Self::collect_paths(repo_path);
        if paths.is_empty() {
            return Ok(None);
        }
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        let primary = json::load_object(&paths[0])?;
        ctx.version = json::str_field(&primary, "version");
        // 根 pyproject 的版本是整仓主源，优先于 package.json 自带的版本
        if let Some(root) = sync::root_pyproject_version(repo_path) {
            ctx.version = Some(root);
        }
        Ok(Some(Self {
            ctx,
            package_json_paths: paths,
            primary_cfg: funbuild_cfg(&primary),
        }))
    }

    fn collect_paths(repo_path: &Path) -> Vec<PathBuf> {
        sync::manifest_paths(repo_path, "package.json")
            .into_iter()
            .filter(|path| match json::load_object(path) {
                Ok(pkg) => package_qualifies(&pkg),
                Err(error) => {
                    log::warn!("skip {}: {error}", path.display());
                    false
                }
            })
            .collect()
    }

    fn rel_dir(&self, path: &Path) -> String {
        let dir = path.parent().unwrap_or(Path::new("."));
        match dir.strip_prefix(&self.ctx.repo_path) {
            Ok(rel) if rel.as_os_str().is_empty() => ".".to_string(),
            Ok(rel) => rel.display().to_string(),
            Err(_) => ".".to_string(),
        }
    }

    fn latest_dependency_cmds(&self) -> Result<Vec<String>> {
        let name_pattern =
            regex::Regex::new(r"^(?:@[a-zA-Z0-9~][a-zA-Z0-9._~-]*/)?[a-zA-Z0-9~][a-zA-Z0-9._~-]*$")
                .expect("valid npm package name regex");
        let mut commands = Vec::new();
        for path in &self.package_json_paths {
            let pkg = json::load_object(path)?;
            let cfg = funbuild_cfg(&pkg);
            let Some(value) = cfg.get("latest-packages") else {
                continue;
            };
            let invalid = || {
                Error::latest_dependency(format!(
                    "{}: funbuild.latest-packages 必须是包名字符串数组（不带版本号）",
                    path.display()
                ))
            };
            let names = value.as_array().ok_or_else(invalid)?;
            let dir = path.parent().unwrap_or(&self.ctx.repo_path);
            let pm = detect_pm(dir, &cfg, &pkg);
            if !names.is_empty() && pm != "pnpm" {
                return Err(Error::latest_dependency(format!(
                    "{}: funbuild.latest-packages 目前仅支持 pnpm，检测到 {pm}",
                    path.display()
                )));
            }
            let mut seen = std::collections::BTreeSet::new();
            for value in names {
                let name = value
                    .as_str()
                    .filter(|n| name_pattern.is_match(n))
                    .ok_or_else(invalid)?;
                if pkg.get("name").and_then(Value::as_str) == Some(name) || !seen.insert(name) {
                    continue;
                }
                let latest = format!("{name}@latest");
                let mut args = vec!["pnpm", "add", &latest];
                if pkg
                    .get("peerDependencies")
                    .and_then(Value::as_object)
                    .is_some_and(|deps| deps.contains_key(name))
                {
                    args.push("--save-peer");
                }
                commands.push(in_dir_shell(&self.rel_dir(path), &shell::join(args)));
            }
        }
        Ok(commands)
    }

    fn install_cmds(pkg_dir: &Path, cfg: &Map<String, Value>, pm: &str) -> Vec<String> {
        if let Some(custom) = cfg_str(cfg, "install") {
            return vec![custom];
        }
        let command = match pm {
            "pnpm" if pkg_dir.join("pnpm-lock.yaml").exists() => "pnpm install --frozen-lockfile",
            "pnpm" => "pnpm install",
            "yarn" if pkg_dir.join("yarn.lock").exists() => "yarn install --frozen-lockfile",
            "yarn" => "yarn install",
            _ if pkg_dir.join("package-lock.json").exists() => "npm ci",
            _ => "npm install",
        };
        vec![command.to_string()]
    }

    fn build_cmd(cfg: &Map<String, Value>, pm: &str) -> String {
        if let Some(custom) = cfg_str(cfg, "build") {
            return custom;
        }
        if pm == "yarn" {
            return "yarn run build".to_string();
        }
        format!("{pm} run build")
    }

    /// 把构建产物打包再全局装上，而不是 `npm install -g .`——后者在新版 npm/pnpm 下
    /// 会软链到源码目录，绕开 package.json 的 files 字段。
    fn global_install_cmd(cfg: &Map<String, Value>, pm: &str) -> String {
        if let Some(custom) = cfg_str(cfg, "globalInstall") {
            return custom;
        }
        let pack = match pm {
            "pnpm" => "pnpm pack",
            "yarn" => "yarn pack",
            _ => "npm pack",
        };
        format!("{pack} && npm install -g \"$(ls -t *.tgz | head -n1)\" && rm -f *.tgz")
    }

    /// 逐个包跑闭包，入参是 (相对目录, 绝对目录, package.json 内容, funbuild 配置, 包管理器)。
    fn for_each_package<T>(
        &self,
        mut f: impl FnMut(&str, &Path, &Map<String, Value>, &Map<String, Value>, &str) -> Vec<T>,
    ) -> Result<Vec<T>> {
        let mut out = Vec::new();
        for path in &self.package_json_paths {
            let rel = self.rel_dir(path);
            let dir = path.parent().unwrap_or(&self.ctx.repo_path).to_path_buf();
            let pkg = json::load_object(path)?;
            let cfg = funbuild_cfg(&pkg);
            let pm = detect_pm(&dir, &cfg, &pkg);
            out.extend(f(&rel, &dir, &pkg, &cfg, &pm));
        }
        Ok(out)
    }

    fn publish_cmds_for(
        &self,
        rel: &str,
        pkg: &Map<String, Value>,
        cfg: &Map<String, Value>,
        pm: &str,
        path: &Path,
    ) -> Vec<String> {
        if cfg.get("publish") == Some(&Value::Bool(false)) {
            return Vec::new();
        }
        if let Some(custom) = cfg_str(cfg, "publish") {
            return vec![in_dir_shell(rel, &custom)];
        }
        if pkg.get("private") == Some(&Value::Bool(true)) {
            log::info!(
                "{}: private=true, skip publish; set funbuild.publish to override",
                path.display()
            );
            return Vec::new();
        }
        let command = match pm {
            "pnpm" => "pnpm publish --no-git-checks",
            "yarn" => "yarn npm publish",
            _ => "npm publish",
        };
        vec![in_dir_shell(rel, command)]
    }
}

impl Builder for NpmFrontendBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "NpmFrontendBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        for path in &self.package_json_paths {
            let mut pkg = json::load_object(path)?;
            pkg.insert("version".into(), Value::String(version.clone()));
            json::save(&pkg, path)?;
        }
        sync::sync_all(&self.ctx.repo_path, &version)?;
        let primary = json::load_object(&self.package_json_paths[0])?;
        self.primary_cfg = funbuild_cfg(&primary);
        Ok(())
    }

    fn cmd_build(&self) -> Result<Vec<String>> {
        self.for_each_package(|rel, dir, _pkg, cfg, pm| {
            let mut out: Vec<String> = Self::install_cmds(dir, cfg, pm)
                .iter()
                .map(|cmd| in_dir_shell(rel, cmd))
                .collect();
            out.push(in_dir_shell(rel, &Self::build_cmd(cfg, pm)));
            out
        })
    }

    fn sync_latest_dependencies(&mut self) -> Result<()> {
        shell::run_checked(&self.latest_dependency_cmds()?, self.ctx.cwd())
    }

    /// 带 bin 字段的包才本地全局装一份。
    fn cmd_install(&self) -> Result<Vec<String>> {
        self.for_each_package(|rel, _dir, pkg, cfg, pm| {
            if !package_has_bin(pkg) || cfg.get("globalInstall") == Some(&Value::Bool(false)) {
                return Vec::new();
            }
            vec![in_dir_shell(rel, &Self::global_install_cmd(cfg, pm))]
        })
    }

    fn cmd_publish(&self) -> Result<Vec<String>> {
        let mut out = Vec::new();
        for path in &self.package_json_paths {
            let rel = self.rel_dir(path);
            let dir = path.parent().unwrap_or(&self.ctx.repo_path).to_path_buf();
            let pkg = json::load_object(path)?;
            let cfg = funbuild_cfg(&pkg);
            let pm = detect_pm(&dir, &cfg, &pkg);
            out.extend(self.publish_cmds_for(&rel, &pkg, &cfg, &pm, path));
        }
        Ok(out)
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        if let Some(Value::Array(dirs)) = self.primary_cfg.get("cleanDirs") {
            if !dirs.is_empty() {
                // 用户配置直接拼进 `rm -rf` 等于把 shell 交给清单文件。全部非法时返回
                // 空列表而不是退回默认项——明确配置过就不该偷偷删别的目录。
                return Ok(dirs
                    .iter()
                    .filter_map(|d| d.as_str())
                    .filter_map(|d| safe_clean_dir(d, "package.json funbuild.cleanDirs"))
                    .map(|d| format!("rm -rf {d}"))
                    .collect());
            }
        }
        Ok([
            "rm -rf dist",
            "rm -rf build",
            "rm -rf .next",
            "rm -rf out",
            "rm -rf storybook-static",
            "rm -rf extbuild/*/dist",
            "rm -rf extbuild/*/build",
            "rm -rf extbuild/*/.next",
            "rm -rf extbuild/*/out",
            "rm -rf extbuild/*/storybook-static",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect())
    }

    /// 装依赖；对带 bin 字段的 CLI 包再构建并本地全局装一份，跟 UVBuild 那边
    /// build -> 本地装 wheel 的路数对齐。
    fn install(&mut self) -> Result<()> {
        log::info!("{} install (frontend dependencies)", self.ctx.name);
        if self.package_json_paths.is_empty() {
            return Ok(());
        }
        let deps = self.for_each_package(|rel, dir, _pkg, cfg, pm| {
            Self::install_cmds(dir, cfg, pm)
                .iter()
                .map(|cmd| in_dir_shell(rel, cmd))
                .collect()
        })?;
        shell::run_checked(&deps, self.ctx.cwd())?;

        let builds = self.for_each_package(|rel, _dir, pkg, cfg, pm| {
            if !package_has_bin(pkg) {
                return Vec::new();
            }
            vec![in_dir_shell(rel, &Self::build_cmd(cfg, pm))]
        })?;
        if !builds.is_empty() {
            shell::run_checked(&builds, self.ctx.cwd())?;
            let install = self.cmd_install()?;
            shell::run_checked(&install, self.ctx.cwd())?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkg_json(dir: &Path, body: &str) {
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join("package.json"), body).unwrap();
    }

    const BUILDABLE: &str =
        r#"{"name": "demo", "version": "1.0.0", "scripts": {"build": "vite build"}}"#;

    #[test]
    fn detects_package_with_build_script() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.0.0"));
    }

    #[test]
    fn ignores_packages_without_version_or_build() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), r#"{"name": "demo", "scripts": {"build": "x"}}"#);
        assert!(NpmFrontendBuild::detect(dir.path()).unwrap().is_none());

        pkg_json(dir.path(), r#"{"name": "demo", "version": "1.0.0"}"#);
        assert!(NpmFrontendBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn custom_build_command_qualifies_without_scripts() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(
            dir.path(),
            r#"{"name": "demo", "version": "1.0.0", "funbuild": {"build": "make web"}}"#,
        );
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_build().unwrap().last().unwrap(), "make web");
    }

    #[test]
    fn root_pyproject_version_wins() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nversion = \"9.9.9\"\n",
        )
        .unwrap();
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("9.9.9"));
    }

    #[test]
    fn package_manager_from_lockfile_then_config() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        std::fs::write(dir.path().join("pnpm-lock.yaml"), "").unwrap();
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_build().unwrap();
        assert_eq!(commands[0], "pnpm install --frozen-lockfile");
        assert_eq!(commands[1], "pnpm run build");

        // 显式配置优先于 lockfile
        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","scripts":{"build":"x"},"funbuild":{"packageManager":"yarn@4.1.0"}}"#,
        );
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_build().unwrap()[1], "yarn run build");
    }

    #[test]
    fn npm_ci_only_with_lockfile() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_build().unwrap()[0], "npm install");

        std::fs::write(dir.path().join("package-lock.json"), "{}").unwrap();
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_build().unwrap()[0], "npm ci");
    }

    #[test]
    fn latest_packages_use_manifest_package_manager_without_lockfile() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","packageManager":"pnpm@10.0.0","scripts":{"build":"x"},"funbuild":{"latest-packages":["@org/sdk","new-package","@org/sdk","demo"]}}"#,
        );
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.latest_dependency_cmds().unwrap(),
            vec![
                "pnpm add '@org/sdk@latest'",
                "pnpm add 'new-package@latest'"
            ]
        );
        assert_eq!(build.cmd_build().unwrap()[0], "pnpm install");
    }

    #[test]
    fn latest_packages_are_local_to_each_package_and_keep_custom_install() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        let child = dir.path().join("extbuild/web");
        pkg_json(
            &child,
            r#"{"name":"web","version":"1.0.0","scripts":{"build":"x"},"peerDependencies":{"react":"^18"},"funbuild":{"install":"custom-install","latest-packages":["react","vite"]}}"#,
        );
        std::fs::write(child.join("pnpm-lock.yaml"), "").unwrap();
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.latest_dependency_cmds().unwrap(),
            vec![
                "(cd extbuild/web && pnpm add 'react@latest' --save-peer)",
                "(cd extbuild/web && pnpm add 'vite@latest')",
            ]
        );
        assert!(build
            .cmd_build()
            .unwrap()
            .contains(&"(cd extbuild/web && custom-install)".into()));
    }

    #[test]
    fn latest_packages_reject_invalid_names_and_config() {
        let dir = tempfile::tempdir().unwrap();
        for invalid in [
            serde_json::json!("react"),
            serde_json::json!(null),
            serde_json::json!([42]),
            serde_json::json!([""]),
            serde_json::json!(["react@19"]),
            serde_json::json!(["--global"]),
            serde_json::json!(["@org/*"]),
            serde_json::json!(["a; touch injected"]),
            serde_json::json!(["file:../pkg"]),
        ] {
            let mut pkg: Value = serde_json::from_str(BUILDABLE).unwrap();
            pkg["packageManager"] = "pnpm@10.0.0".into();
            pkg["funbuild"] = serde_json::json!({"latest-packages": invalid});
            pkg_json(dir.path(), &pkg.to_string());
            let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
            let error = build.latest_dependency_cmds().unwrap_err().to_string();
            assert!(
                error.contains("package.json") && error.contains("latest-packages"),
                "{error}"
            );
        }
    }

    #[test]
    fn latest_packages_require_pnpm_but_empty_or_absent_config_is_a_noop() {
        let dir = tempfile::tempdir().unwrap();
        for cfg in [
            serde_json::json!({}),
            serde_json::json!({"latest-packages": []}),
        ] {
            let mut pkg: Value = serde_json::from_str(BUILDABLE).unwrap();
            pkg["funbuild"] = cfg;
            pkg_json(dir.path(), &pkg.to_string());
            let mut build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
            assert!(build.latest_dependency_cmds().unwrap().is_empty());
            build.sync_latest_dependencies().unwrap();
        }
        let mut pkg: Value = serde_json::from_str(BUILDABLE).unwrap();
        pkg["packageManager"] = "pnpm@10.0.0".into();
        pkg["funbuild"] =
            serde_json::json!({"packageManager": "npm", "latest-packages": ["react"]});
        pkg_json(dir.path(), &pkg.to_string());
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert!(build
            .latest_dependency_cmds()
            .unwrap_err()
            .to_string()
            .contains("检测到 npm"));
    }

    #[test]
    fn subpackages_run_in_subshells() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        pkg_json(&dir.path().join("extbuild/web"), BUILDABLE);
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_build().unwrap();
        assert!(
            commands
                .iter()
                .any(|c| c == "(cd extbuild/web && npm run build)"),
            "{commands:?}"
        );
        // 根包不套子 shell
        assert!(
            commands.contains(&"npm run build".to_string()),
            "{commands:?}"
        );
    }

    #[test]
    fn private_packages_are_not_published() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","private":true,"scripts":{"build":"x"}}"#,
        );
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        assert!(build.cmd_publish().unwrap().is_empty());
    }

    #[test]
    fn publish_can_be_disabled_or_overridden() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","scripts":{"build":"x"},"funbuild":{"publish":false}}"#,
        );
        assert!(NpmFrontendBuild::detect(dir.path())
            .unwrap()
            .unwrap()
            .cmd_publish()
            .unwrap()
            .is_empty());

        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","scripts":{"build":"x"},"funbuild":{"publish":"./deploy.sh"}}"#,
        );
        assert_eq!(
            NpmFrontendBuild::detect(dir.path())
                .unwrap()
                .unwrap()
                .cmd_publish()
                .unwrap(),
            vec!["./deploy.sh"]
        );
    }

    #[test]
    fn publish_uses_package_manager_specific_command() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        std::fs::write(dir.path().join("pnpm-lock.yaml"), "").unwrap();
        assert_eq!(
            NpmFrontendBuild::detect(dir.path())
                .unwrap()
                .unwrap()
                .cmd_publish()
                .unwrap(),
            vec!["pnpm publish --no-git-checks"]
        );
    }

    #[test]
    fn only_cli_packages_get_globally_installed() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        assert!(NpmFrontendBuild::detect(dir.path())
            .unwrap()
            .unwrap()
            .cmd_install()
            .unwrap()
            .is_empty());

        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","bin":{"demo":"./cli.js"},"scripts":{"build":"x"}}"#,
        );
        let commands = NpmFrontendBuild::detect(dir.path())
            .unwrap()
            .unwrap()
            .cmd_install()
            .unwrap();
        assert_eq!(commands.len(), 1);
        assert!(commands[0].contains("npm pack"), "{commands:?}");
        assert!(commands[0].contains("npm install -g"), "{commands:?}");
    }

    #[test]
    fn clean_dirs_are_validated() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(
            dir.path(),
            r#"{"name":"demo","version":"1.0.0","scripts":{"build":"x"},"funbuild":{"cleanDirs":["dist","/etc","a; rm -rf /"]}}"#,
        );
        let build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        // 非法项被丢掉，但不退回默认清理项
        assert_eq!(build.cmd_delete().unwrap(), vec!["rm -rf dist"]);
    }

    #[test]
    fn version_written_to_every_package_json() {
        let dir = tempfile::tempdir().unwrap();
        pkg_json(dir.path(), BUILDABLE);
        pkg_json(&dir.path().join("exts/admin"), BUILDABLE);
        let mut build = NpmFrontendBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        for path in ["package.json", "exts/admin/package.json"] {
            let body = std::fs::read_to_string(dir.path().join(path)).unwrap();
            assert!(body.contains("\"version\": \"1.0.1\""), "{path}: {body}");
        }
    }
}
