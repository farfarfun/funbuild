use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use ini::Ini;
use toml_edit::DocumentMut;

use crate::builders::{default_delete_commands, Builder, RepoContext};
use crate::error::Result;
use crate::latest_deps;
use crate::manifest::sync;
use crate::manifest::toml as toml_manifest;
use crate::shell;

/// LICENSE 文件的常见命名，用于填充 PEP 639 的 project.license-files。
const LICENSE_FILE_NAMES: [&str; 4] = ["LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING"];

const ORG_PEOPLE: [(&str, &str); 2] = [
    ("牛哥", "niuliangtao@qq.com"),
    ("farfarfun", "farfarfun@qq.com"),
];

/// 多包构建时各包 wheel 的输出目录（相对仓库根），互不覆盖。
pub(crate) fn bundle_out_dir(pkg_dir: &str) -> String {
    let key = if pkg_dir == "." || pkg_dir.is_empty() {
        "root".to_string()
    } else {
        pkg_dir.replace('/', "_")
    };
    format!("dist/funbuild/{key}")
}

/// 同 [`bundle_out_dir`] 但为绝对路径。
///
/// `uv build --directory <子目录>` 时 `--out-dir` 的相对路径按「包目录」解析，传
/// 相对路径会把产物写到 `extbuild/foo/dist/...` 而非仓库根下，后续 publish 就找不到了。
fn bundle_out_dir_abs(repo: &Path, pkg_dir: &str) -> String {
    repo.join(bundle_out_dir(pkg_dir)).display().to_string()
}

/// 使用 uv / PEP 621 标准 pyproject.toml 的现代 Python 包（含 extbuild/exts 子包）。
pub struct UvBuild {
    pub(crate) ctx: RepoContext,
    /// 根清单在 [0]，其后是 extbuild/exts 下的子包清单。相对仓库根的路径。
    pub(crate) toml_paths: Vec<PathBuf>,
}

impl UvBuild {
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let root = repo_path.join("pyproject.toml");
        if !root.exists() {
            return Ok(None);
        }
        // 清单写坏了不能当成「不是 uv 项目」，错误往上抛由探测链记账
        let doc = toml_manifest::load(&root)?;
        if !toml_manifest::has_table(&doc, &["project"]) {
            return Ok(None);
        }

        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        // name 默认取目录名，但 pyproject 改名后目录名往往还没同步，
        // [project].name 才是包的真实身份。
        if let Some(name) = toml_manifest::dig_nonempty(&doc, &["project", "name"]) {
            ctx.name = name;
        }
        // 版本始终以根 [project].version 为主源
        ctx.version = Some(
            sync::root_pyproject_version(repo_path)
                .or_else(|| toml_manifest::dig_nonempty(&doc, &["project", "version"]))
                .unwrap_or_else(|| "0.0.1".to_string()),
        );

        let mut toml_paths = vec![root];
        toml_paths.extend(
            sync::manifest_paths(repo_path, "pyproject.toml")
                .into_iter()
                .skip_while(|path| path == &repo_path.join("pyproject.toml")),
        );
        Ok(Some(Self { ctx, toml_paths }))
    }

    /// `[tool.uv] package = false` 声明的虚拟项目：有依赖、有脚本，但本身不是可安装包。
    ///
    /// 这类项目一样需要 `uv sync` 拉依赖，版本同步、push、打 tag 都照常做，但
    /// `uv build` 对它必然失败（没有 build-system 时 uv 回退到 setuptools，找不到
    /// 任何包就报错），于是整条发版链在这个仓库上中断。
    ///
    /// 解析失败按「不是虚拟项目」处理：清单读不出来的后果由后面真正解析它的
    /// write_version 去报。
    fn is_virtual(path: &Path) -> bool {
        match toml_manifest::load(path) {
            Ok(doc) => toml_manifest::dig_bool(&doc, &["tool", "uv", "package"]) == Some(false),
            Err(error) => {
                log::warn!(
                    "无法判断 {} 是否为 uv 虚拟项目, 按可构建处理: {error}",
                    path.display()
                );
                false
            }
        }
    }

    /// 相对仓库根的包目录，按 toml_paths 顺序去重，排除虚拟项目。
    pub(crate) fn buildable_pkg_dirs(&self) -> Vec<String> {
        let mut dirs: Vec<String> = Vec::new();
        for path in &self.toml_paths {
            let pkg_dir = self.rel_pkg_dir(path);
            if dirs.contains(&pkg_dir) {
                continue;
            }
            if Self::is_virtual(path) {
                log::info!(
                    "skip build/publish, uv 虚拟项目 (package = false): {}",
                    path.display()
                );
                continue;
            }
            dirs.push(pkg_dir);
        }
        dirs
    }

    fn rel_pkg_dir(&self, toml_path: &Path) -> String {
        let dir = toml_path.parent().unwrap_or(Path::new("."));
        match dir.strip_prefix(&self.ctx.repo_path) {
            Ok(rel) if rel.as_os_str().is_empty() => ".".to_string(),
            Ok(rel) => rel.display().to_string(),
            Err(_) => ".".to_string(),
        }
    }

    /// 按 PEP 639 (setuptools>=77) 声明许可证。
    ///
    /// 旧写法 `[tool.setuptools] license-files = []` 会让 wheel 不带许可证文件，且与
    /// `[project].license-files` 同时存在会直接报错；`License ::` classifier 与 SPDX
    /// 的 project.license 也不能共存，一并清掉。
    fn apply_license_metadata(doc: &mut DocumentMut, pkg_dir: &Path) {
        if let Some(setuptools) = toml_manifest::dig_table_mut(doc, &["tool", "setuptools"]) {
            setuptools.remove("license-files");
        }

        let project = match toml_manifest::ensure_table(doc, &["project"]) {
            Ok(project) => project,
            Err(error) => {
                log::warn!("跳过许可证元数据: {error}");
                return;
            }
        };

        // 写 project.license 之前必须把 License classifier 摘掉，否则 setuptools>=77
        // 直接中止构建：License classifiers have been superseded by license expressions。
        if let Some(classifiers) = project
            .get_mut("classifiers")
            .and_then(|i| i.as_array_mut())
        {
            let dropped: Vec<String> = classifiers
                .iter()
                .filter_map(|c| c.as_str())
                .filter(|c| c.starts_with("License ::"))
                .map(|c| c.to_string())
                .collect();
            if !dropped.is_empty() {
                log::info!("移除已被 PEP 639 取代的 License classifier: {dropped:?}");
                classifiers.retain(|c| !c.as_str().is_some_and(|s| s.starts_with("License ::")));
            }
        }

        // 旧的 license = {text = "MIT"} 表写法在 PEP 639 下已废弃，统一为 SPDX 字符串
        let license = project.get("license");
        let as_str = license
            .and_then(|item| item.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty());
        if as_str.is_none() {
            let from_table = license
                .and_then(|item| item.as_table_like())
                .and_then(|table| table.get("text"))
                .and_then(|text| text.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or("MIT")
                .to_string();
            project.insert("license", toml_edit::value(from_table));
        }

        let found: Vec<&str> = LICENSE_FILE_NAMES
            .iter()
            .copied()
            .filter(|name| pkg_dir.join(name).is_file())
            .collect();
        if found.is_empty() {
            // 声明了却找不到文件同样会让 setuptools 构建失败
            project.remove("license-files");
        } else {
            project.insert(
                "license-files",
                toml_edit::Item::Value(toml_manifest::string_array(found).into()),
            );
        }
    }

    /// 按组织约定补齐 pyproject 元信息（许可证 / authors / urls / description）。
    pub(crate) fn config_format(&self, doc: &mut DocumentMut, pkg_dir: &Path) {
        if !self.ctx.is_org_repo() {
            return;
        }
        Self::apply_license_metadata(doc, pkg_dir);

        let people: Vec<Vec<(&str, String)>> = ORG_PEOPLE
            .iter()
            .map(|(name, email)| vec![("name", name.to_string()), ("email", email.to_string())])
            .collect();
        let name = self.ctx.name.clone();
        let Ok(project) = toml_manifest::ensure_table(doc, &["project"]) else {
            return;
        };
        toml_manifest::set_table_array(project, "authors", &people);
        toml_manifest::set_table_array(project, "maintainers", &people);
        toml_manifest::set_subtable(
            project,
            "urls",
            &[
                ("Organization", "https://github.com/farfarfun".to_string()),
                ("Repository", format!("https://github.com/farfarfun/{name}")),
                (
                    "Releases",
                    format!("https://github.com/farfarfun/{name}/releases"),
                ),
            ],
        );

        // description 是可选字段：uv init 之外的模板未必写它
        let description = project.get("description").and_then(|item| item.as_str());
        if description.is_none_or(|text| text.contains("Add your description here")) {
            toml_manifest::set_value(project, "description", name.as_str());
        }
    }

    /// 把 ~/.pypirc 凭据导出为 UV_PUBLISH_* 环境变量。
    ///
    /// 凭据不能拼进命令行：完整命令行会出现在进程表里对本机所有用户可见。按 SPEC
    /// §9.3 的优先级（环境变量 > 配置文件），已显式导出的不覆盖，只补齐缺失的部分。
    fn export_publish_credentials(settings: Option<&ini::Properties>) {
        let Some(settings) = settings else { return };
        let Some(user) = settings.get("username") else {
            return;
        };
        let password = settings.get("password");

        if user.contains("__token__") {
            if let Some(password) = password {
                if std::env::var_os("UV_PUBLISH_TOKEN").is_none() {
                    std::env::set_var("UV_PUBLISH_TOKEN", password);
                }
            }
        } else {
            if std::env::var_os("UV_PUBLISH_USERNAME").is_none() {
                std::env::set_var("UV_PUBLISH_USERNAME", user);
            }
            if let Some(password) = password {
                if std::env::var_os("UV_PUBLISH_PASSWORD").is_none() {
                    std::env::set_var("UV_PUBLISH_PASSWORD", password);
                }
            }
        }
        if let Some(url) = settings.get("repository") {
            if std::env::var_os("UV_PUBLISH_URL").is_none() {
                std::env::set_var("UV_PUBLISH_URL", url);
            }
        }
    }

    /// `[[tool.uv.index]]` 的第一个 name，表数组和内联数组两种写法都认。
    fn first_uv_index_name(doc: &DocumentMut) -> Option<String> {
        let item = toml_manifest::dig(doc, &["tool", "uv", "index"])?;
        if let Some(tables) = item.as_array_of_tables() {
            return tables.get(0)?.get("name")?.as_str().map(str::to_string);
        }
        item.as_array()?
            .get(0)?
            .as_inline_table()?
            .get("name")?
            .as_str()
            .map(str::to_string)
    }
}

impl Builder for UvBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "UVBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        for path in self.toml_paths.clone() {
            let pkg_dir = path.parent().unwrap_or(&self.ctx.repo_path).to_path_buf();
            let result = (|| -> Result<()> {
                let mut doc = toml_manifest::load(&path)?;
                self.config_format(&mut doc, &pkg_dir);
                let project = toml_manifest::ensure_table(&mut doc, &["project"])?;
                toml_manifest::set_value(project, "version", version.as_str());
                toml_manifest::save(&doc, &path)
            })();
            if let Err(error) = result {
                log::error!("Failed to update version in {}: {error}", path.display());
                return Err(error);
            }
        }
        sync::sync_all(&self.ctx.repo_path, &version)
    }

    /// 把 `[tool.funbuild].latest-packages` 里的依赖下界抬到最新已发布版本。
    fn sync_latest_dependencies(&mut self) -> Result<()> {
        let changed = latest_deps::sync_latest_dependencies(&self.ctx.repo_path, &self.toml_paths)?;
        for path in changed {
            log::info!("latest-packages: 已更新依赖下界 {}", path.display());
        }
        Ok(())
    }

    /// 先删 uv.lock 再 `uv lock`：组织内的依赖刚刚才各自发了新版，保留旧 lock 的话
    /// `uv lock` 只做增量更新，会把上一轮解析出的旧版本继续钉住。
    ///
    /// 这一步必须留在构建之前，不能放进 cmd_delete——那个方法在 publish 之后还会
    /// 再跑一次、跟着 push 走，等于把仓库里该提交的 uv.lock 删掉。
    fn cmd_build(&self) -> Result<Vec<String>> {
        let mut result = vec![
            "rm -rf uv.lock".to_string(),
            "uv lock --prerelease=allow".to_string(),
        ];
        if self.ctx.is_org_repo() {
            result.push("uvx ruff format".to_string());
            result.push("uvx ruff clean".to_string());
        }
        for pkg_dir in self.buildable_pkg_dirs() {
            let out_dir = bundle_out_dir_abs(&self.ctx.repo_path, &pkg_dir);
            result.push(format!(
                "uv build -q --wheel --prerelease=allow --directory {} --out-dir {} --clear",
                shell::quote(&pkg_dir),
                shell::quote(&out_dir)
            ));
        }
        Ok(result)
    }

    fn cmd_publish(&self) -> Result<Vec<String>> {
        let pkg_dirs = self.buildable_pkg_dirs();
        // 整仓都是虚拟项目时提前返回：没东西要上传，就不该顺手把 ~/.pypirc 里的
        // 凭据导出到环境变量，让它被后续无关的子进程继承。
        if pkg_dirs.is_empty() {
            return Ok(Vec::new());
        }

        let pypirc = std::env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join(".pypirc"));
        let config = pypirc
            .filter(|path| path.exists())
            .and_then(|path| Ini::load_from_file(path).ok());

        let mut server = "pypi".to_string();
        if let Some(config) = &config {
            if let Some(servers) = config
                .section(Some("distutils"))
                .and_then(|s| s.get("index-servers"))
            {
                if let Some(first) = servers.split_whitespace().next() {
                    server = first.to_string();
                }
            }
        }
        if let Ok(doc) = toml_manifest::load(&self.toml_paths[0]) {
            if let Some(name) = Self::first_uv_index_name(&doc) {
                server = name;
            }
        }
        log::info!("public server: {server}");
        Self::export_publish_credentials(config.as_ref().and_then(|c| c.section(Some(&server))));

        Ok(pkg_dirs
            .iter()
            .map(|pkg_dir| {
                let out_dir = bundle_out_dir_abs(&self.ctx.repo_path, pkg_dir);
                format!("uv publish {}", shell::quote(&format!("{out_dir}/*")))
            })
            .collect())
    }

    /// 整仓都是虚拟项目时没有 wheel，`dist/funbuild/*/*.whl` 这个 glob 会原样传给 uv
    /// 并报「找不到文件」，把一次本该什么都不做的发版变成失败。
    fn cmd_install(&self) -> Result<Vec<String>> {
        if self.buildable_pkg_dirs().is_empty() {
            return Ok(Vec::new());
        }
        Ok(vec!["uv pip install dist/funbuild/*/*.whl".to_string()])
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        let mut result = default_delete_commands();
        result.extend(
            [
                "rm -rf src/*.egg-info",
                "rm -rf extbuild/*/src/*.egg-info",
                "rm -rf exts/*/src/*.egg-info",
            ]
            .iter()
            .map(|s| s.to_string()),
        );
        Ok(result)
    }

    /// 每个 pyproject 的 [project].name -> 它所在的目录。
    ///
    /// 分发名得按清单读，不能用目录名：重命名后目录名往往还没跟上。
    fn published_distributions(&self) -> Result<BTreeMap<String, PathBuf>> {
        let buildable = self.buildable_pkg_dirs();
        if buildable.is_empty() {
            return Ok(BTreeMap::new());
        }
        let mut found = BTreeMap::new();
        for path in &self.toml_paths {
            if !buildable.contains(&self.rel_pkg_dir(path)) {
                continue;
            }
            let doc = toml_manifest::load(path)?;
            if let Some(name) = toml_manifest::dig_nonempty(&doc, &["project", "name"]) {
                let dir = path.parent().unwrap_or(&self.ctx.repo_path);
                found.insert(
                    name,
                    dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf()),
                );
            }
        }
        if found.is_empty() {
            let ctx = self.ctx();
            return Ok([(ctx.name.clone(), ctx.repo_path.clone())].into());
        }
        Ok(found)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn uv_repo(dir: &Path, body: &str) {
        std::fs::write(dir.join("pyproject.toml"), body).unwrap();
    }

    #[test]
    fn detects_pep621_project() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo-pkg\"\nversion = \"1.2.3\"\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.2.3"));
        // name 取 [project].name 而不是目录名
        assert_eq!(build.ctx().name, "demo-pkg");
    }

    #[test]
    fn requires_project_table() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(dir.path(), "[tool.ruff]\nline-length = 120\n");
        assert!(UvBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn missing_version_defaults_to_initial() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(dir.path(), "[project]\nname = \"demo\"\n");
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("0.0.1"));
    }

    #[test]
    fn collects_subpackages() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"root\"\nversion = \"1.0.0\"\n",
        );
        for sub in ["extbuild/one", "exts/two"] {
            std::fs::create_dir_all(dir.path().join(sub)).unwrap();
            uv_repo(
                &dir.path().join(sub),
                "[project]\nname = \"sub\"\nversion = \"1.0.0\"\n",
            );
        }
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.toml_paths.len(), 3);
        assert_eq!(
            build.buildable_pkg_dirs(),
            vec![".", "extbuild/one", "exts/two"]
        );
    }

    #[test]
    fn excludes_virtual_projects() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n\n[tool.uv]\npackage = false\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        // 仍被认领（版本同步、push、tag 照常），但不构建不发布
        assert!(build.buildable_pkg_dirs().is_empty());
        assert!(build.cmd_install().unwrap().is_empty());
        assert!(build.cmd_publish().unwrap().is_empty());
        assert!(build.published_distributions().unwrap().is_empty());
    }

    #[test]
    fn out_dirs_are_unique_per_package() {
        assert_eq!(bundle_out_dir("."), "dist/funbuild/root");
        assert_eq!(bundle_out_dir("extbuild/one"), "dist/funbuild/extbuild_one");
    }

    #[test]
    fn build_commands_refresh_lock_then_build_each_package() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_build().unwrap();
        assert_eq!(commands[0], "rm -rf uv.lock");
        assert_eq!(commands[1], "uv lock --prerelease=allow");
        let last = commands.last().unwrap();
        assert!(
            last.starts_with("uv build -q --wheel --prerelease=allow --directory ."),
            "{last}"
        );
        assert!(last.contains("dist/funbuild/root"), "{last}");
    }

    #[test]
    fn delete_never_touches_lock_files() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_delete().unwrap();
        assert!(
            !commands.iter().any(|c| c.contains("uv.lock")),
            "{commands:?}"
        );
        assert!(commands.contains(&"rm -rf dist".to_string()));
    }

    #[test]
    fn version_written_to_every_manifest() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        std::fs::create_dir_all(dir.path().join("extbuild/one")).unwrap();
        uv_repo(
            &dir.path().join("extbuild/one"),
            "[project]\nname = \"sub\"\nversion = \"0.0.9\"\n",
        );

        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("v2.0.0")).unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("2.0.0"));
        for path in ["pyproject.toml", "extbuild/one/pyproject.toml"] {
            let body = std::fs::read_to_string(dir.path().join(path)).unwrap();
            assert!(body.contains("version = \"2.0.0\""), "{path}: {body}");
        }
    }

    #[test]
    fn org_metadata_uses_table_array_form() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"funthing\"\nversion = \"1.0.0\"\n",
        );
        std::fs::write(dir.path().join("LICENSE"), "MIT").unwrap();

        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();

        // 必须是 [[project.authors]] 而不是 inline 数组，否则每次发版都产生无意义 diff
        assert!(body.contains("[[project.authors]]"), "{body}");
        assert!(body.contains("[[project.maintainers]]"), "{body}");
        assert!(body.contains("[project.urls]"), "{body}");
        assert!(
            body.contains("https://github.com/farfarfun/funthing/releases"),
            "{body}"
        );
        assert!(body.contains("license = \"MIT\""), "{body}");
        assert!(body.contains("license-files = [\"LICENSE\"]"), "{body}");
    }

    #[test]
    fn org_metadata_keeps_comments_written_above_those_keys() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"funthing\"\nversion = \"1.0.0\"\n\n# 为什么这里只列两个人\n[[project.authors]]\nname = \"牛哥\"\nemail = \"niuliangtao@qq.com\"\n\n[[project.authors]]\nname = \"farfarfun\"\nemail = \"farfarfun@qq.com\"\n\n# 这段也得活下来\n[project.urls]\nOrganization = \"https://github.com/farfarfun\"\n",
        );
        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("1.0.1")).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(body.contains("# 为什么这里只列两个人"), "{body}");
        assert!(body.contains("# 这段也得活下来"), "{body}");
    }

    #[test]
    fn org_metadata_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"funthing\"\nversion = \"1.0.0\"\n",
        );
        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("1.0.1")).unwrap();
        let first = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        build.upgrade(Some("1.0.1")).unwrap();
        let second = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert_eq!(first, second, "重复 upgrade 不该继续改动文件");
    }

    #[test]
    fn drops_license_classifier_and_old_setuptools_key() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"funthing\"\nversion = \"1.0.0\"\nclassifiers = [\n    \"License :: OSI Approved :: MIT License\",\n    \"Programming Language :: Python :: 3\",\n]\n\n[tool.setuptools]\nlicense-files = []\n",
        );
        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(!body.contains("License :: OSI Approved"), "{body}");
        assert!(
            body.contains("Programming Language :: Python :: 3"),
            "{body}"
        );
        assert!(!body.contains("[tool.setuptools]\nlicense-files"), "{body}");
    }

    #[test]
    fn converts_legacy_license_table_to_spdx() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"funthing\"\nversion = \"1.0.0\"\nlicense = { text = \"Apache-2.0\" }\n",
        );
        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(body.contains("license = \"Apache-2.0\""), "{body}");
    }

    #[test]
    fn non_org_repo_keeps_metadata_untouched() {
        let dir = tempfile::tempdir().unwrap();
        // 名字不带组织前缀，又没有 remote，is_org_repo 为假
        uv_repo(
            dir.path(),
            "[project]\nname = \"somebody-else\"\nversion = \"1.0.0\"\n",
        );
        let mut build = UvBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(Some("1.0.1")).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pyproject.toml")).unwrap();
        assert!(!body.contains("authors"), "{body}");
        assert!(!body.contains("farfarfun"), "{body}");
        assert!(body.contains("version = \"1.0.1\""), "{body}");
        // 非组织仓库也不跑 ruff
        assert!(!build
            .cmd_build()
            .unwrap()
            .iter()
            .any(|c| c.contains("ruff")));
    }

    #[test]
    fn publish_targets_each_package_bundle() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n\n[[tool.uv.index]]\nname = \"private\"\nurl = \"https://example.com/simple\"\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_publish().unwrap();
        assert_eq!(commands.len(), 1);
        assert!(commands[0].starts_with("uv publish "), "{:?}", commands[0]);
        assert!(
            commands[0].contains("dist/funbuild/root/*"),
            "{:?}",
            commands[0]
        );
    }

    #[test]
    fn reads_index_name_from_both_toml_shapes() {
        let table_array: DocumentMut = "[[tool.uv.index]]\nname = \"aaa\"\n".parse().unwrap();
        assert_eq!(
            UvBuild::first_uv_index_name(&table_array).as_deref(),
            Some("aaa")
        );
        let inline: DocumentMut = "[tool.uv]\nindex = [{ name = \"bbb\", url = \"u\" }]\n"
            .parse()
            .unwrap();
        assert_eq!(
            UvBuild::first_uv_index_name(&inline).as_deref(),
            Some("bbb")
        );
    }

    #[test]
    fn published_distributions_read_names_from_manifests() {
        let dir = tempfile::tempdir().unwrap();
        uv_repo(
            dir.path(),
            "[project]\nname = \"real-name\"\nversion = \"1.0.0\"\n",
        );
        let build = UvBuild::detect(dir.path()).unwrap().unwrap();
        let dists = build.published_distributions().unwrap();
        assert!(dists.contains_key("real-name"), "{dists:?}");
    }
}
