//! 发版时把指定依赖的版本下界抬到「此刻最新的已发布版本」。
//!
//! `rm -rf uv.lock && uv lock` 只影响本仓库构建时解析到的版本，发布出去的 wheel
//! metadata 依然写着早已过期的下界——下游 `pip install -U` 默认
//! `only-if-needed`，已装的旧版满足旧下界就不会被升上来。要真的「每次都最新」，
//! 必须把下界本身写回 pyproject.toml。
//!
//! 只处理 PEP 621 形状的依赖声明（`[project]` 的 dependencies /
//! optional-dependencies，以及 PEP 735 的 `[dependency-groups]`）。

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

use regex::Regex;
use toml_edit::{Array, DocumentMut};

use crate::error::{Error, Result};
use crate::manifest::toml as toml_manifest;
use crate::shell;
use crate::util::normalize_package_name;

/// 独立仓库把配置写在自己的 pyproject.toml 里。
const PYPROJECT_CONFIG_KEY: &str = "latest-packages";
/// `<product>-dev` 编排仓库沿用 SubmoduleWorkspaceBuild 已有的那份配置。
const WORKSPACE_CONFIG_PATH: &str = "scripts/funbuild.toml";
const WORKSPACE_CONFIG_KEY: &str = "packages";

const RESOLVE_TIMEOUT: Duration = Duration::from_secs(300);

/// 这几个运算符表达「从哪个版本起」，抬最新版时要跟着走。`<`/`<=`/`!=`/`===`
/// 是调用方刻意加的上界与排除项，原样保留。
const LOWER_BOUND_OPERATORS: [&str; 4] = ["~=", ">=", ">", "=="];

/// PEP 508 的 `name[extras] <specifiers>` 开头部分。
fn requirement_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?s)^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?(?P<rest>.*)$")
            .unwrap()
    })
}

/// 版本约束的单个子句，如 `>=1.0.0`、`!= 1.2.*`。
fn clause_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*(===|==|!=|<=|>=|~=|<|>)\s*(.+?)\s*$").unwrap())
}

/// `uv pip compile` 输出的锁定行，如 `funflix==1.9.0`。
fn pinned_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#]+)",
        )
        .unwrap()
    })
}

/// 只取根 pyproject.toml 里声明的那一份名单。
///
/// 不含 `scripts/funbuild.toml` 的 `packages`：那是编排仓库「这条链上要发哪些包」
/// 的清单，拿它去补依赖会给根 pyproject 加上一堆它并不依赖的包。
pub fn pyproject_latest_packages(repo: &Path) -> Vec<String> {
    read_string_list(
        &repo.join("pyproject.toml"),
        &["tool", "funbuild", PYPROJECT_CONFIG_KEY],
    )
}

/// 声明了「发版时抬到最新」的包名，两个来源取并集、保持首次出现的顺序。
pub fn latest_packages(repo: &Path) -> Vec<String> {
    let mut names = pyproject_latest_packages(repo);
    names.extend(read_string_list(
        &repo.join(WORKSPACE_CONFIG_PATH),
        &[WORKSPACE_CONFIG_KEY],
    ));
    let mut seen = BTreeSet::new();
    names.retain(|name| seen.insert(name.clone()));
    names
}

/// 解析失败只警告：该不该中止发布由调用方按「有没有声明过 latest-packages」判断，
/// 而这里恰恰是连有没有声明都不知道。
fn read_string_list(path: &Path, keys: &[&str]) -> Vec<String> {
    if !path.is_file() {
        return Vec::new();
    }
    match toml_manifest::load(path) {
        Ok(doc) => toml_manifest::dig_string_list(&doc, keys),
        Err(error) => {
            log::warn!("skip {}: {error}", path.display());
            Vec::new()
        }
    }
}

/// 问 index 要 `package` 当前的最新版本号。
///
/// 用 `uv pip compile` 而不是 PyPI 的 JSON API：它会读 `cwd` 下 pyproject.toml 的
/// `[tool.uv]` 配置，组织内部包发在私有 index 上时同样解析得到。
///
/// `--refresh-package` 不是可选的优化：uv 会缓存 index 的包版本列表，而「刚把上游
/// 发出去、紧接着发下游」正是这个功能存在的理由，缓存里存的是上游发布之前的快照。
pub fn resolve_latest_version(package: &str, cwd: &Path) -> Result<String> {
    let mut requirement = tempfile::Builder::new()
        .prefix("funbuild-latest-")
        .suffix(".in")
        .tempfile()?;
    writeln!(requirement, "{package}")?;
    requirement.flush()?;

    let command = shell::join([
        "uv",
        "pip",
        "compile",
        "--no-header",
        "--no-deps",
        "--prerelease=allow",
        "--refresh-package",
        package,
        &requirement.path().display().to_string(),
    ]);
    let output = shell::capture(&command, Some(cwd), Some(RESOLVE_TIMEOUT));

    let normalized = normalize_package_name(package);
    if let Ok(output) = &output {
        for line in output.stdout.lines() {
            if let Some(caps) = pinned_re().captures(line.trim()) {
                if normalize_package_name(&caps["name"]) == normalized {
                    return Ok(caps["version"].to_string());
                }
            }
        }
    }

    // 失败原因全在 stderr 里（index 没配、私有源 401、requires-python 不匹配），
    // 丢掉它的话报错就只剩一句「无法解析」，看不出该去改什么。
    let detail = match &output {
        Ok(output) => output.stderr.trim().to_string(),
        Err(error) => error.to_string(),
    };
    Err(Error::latest_dependency(format!(
        "无法解析 {package:?} 的最新版本, 拒绝带着过期的依赖下界继续发布。\n\
         命令: {command}\n\
         工作目录: {}\n\
         uv 输出: {}\n\
         若该包只发布在私有 index 上, 得先让 uv 看得见那个 index: 在 {} 里配 \
         [[tool.uv.index]], 或在 ~/.config/uv/uv.toml 里全局配一次 —— funbuild 不会去猜\
         这个地址, `~/.pypirc` 里的 repository 是上传端点, 与解析用的 simple index 不是\
         同一个 URL。",
        cwd.display(),
        if detail.is_empty() { "(无)" } else { &detail },
        cwd.join("pyproject.toml").display(),
    )))
}

/// 把一条依赖声明里的版本下界改写成 `version`；无需改动时返回 None。
///
/// extras、environment marker、上界与排除项都原样保留：最新版为 1.9.0 时
/// `funflix[all]>=1.0,<2; python_version>="3.11"` 变成 `funflix[all]>=1.9.0,<2; ...`。
/// 原本没写任何版本约束的 `funflix` 会补上 `>=1.9.0`。
pub fn rewrite_requirement(requirement: &str, version: &str) -> Option<String> {
    let (head, marker) = match requirement.split_once(';') {
        Some((head, marker)) => (head, Some(marker)),
        None => (requirement, None),
    };
    let caps = requirement_re().captures(head.trim())?;
    let rest = caps["rest"].trim();

    // `funflix @ https://...` 的版本不来自 index，原样留着
    if rest.starts_with('@') {
        return None;
    }
    // PEP 508 允许 `name (>=1.0)` 的带括号写法
    let rest = if rest.starts_with('(') && rest.ends_with(')') {
        rest[1..rest.len() - 1].trim()
    } else {
        rest
    };

    let mut rendered: Vec<String> = Vec::new();
    let mut replaced = false;
    for clause in rest.split(',') {
        if clause.trim().is_empty() {
            continue;
        }
        match clause_re().captures(clause) {
            Some(parts) if LOWER_BOUND_OPERATORS.contains(&&parts[1]) => {
                rendered.push(format!("{}{version}", &parts[1]));
                replaced = true;
            }
            _ => rendered.push(clause.trim().to_string()),
        }
    }
    if !replaced {
        rendered.insert(0, format!(">={version}"));
    }
    // 原文写了多个下界子句（`>=1.0,>=1.2`）时，替换后会得到两条一样的约束
    let mut seen = BTreeSet::new();
    rendered.retain(|clause| seen.insert(clause.clone()));

    let name = &caps["name"];
    let extras = caps.name("extras").map_or("", |m| m.as_str());
    let head = format!("{name}{extras}{}", rendered.join(","));
    let updated = match marker {
        Some(marker) => format!("{head};{marker}"),
        None => head,
    };
    (updated != requirement).then_some(updated)
}

/// 原地改写一个依赖数组，有改动返回 true；命中的包名记入 `declared`。
///
/// `declared` 收的是「这个包已经在某个依赖数组里出现过」，与是否真改写无关——已经是
/// 最新版、或是 `name @ url` 这种不动的形式，同样算声明过。
fn rewrite_array(
    array: &mut Array,
    targets: &BTreeMap<String, String>,
    declared: &mut BTreeSet<String>,
) -> bool {
    let mut changed = false;
    let mut updates: Vec<(usize, String)> = Vec::new();
    for (index, item) in array.iter().enumerate() {
        // `[dependency-groups]` 里可以出现 `{include-group = "..."}` 这种表项
        let Some(dependency) = item.as_str() else {
            continue;
        };
        let dependency = dependency.trim();
        let Some(caps) = requirement_re().captures(dependency) else {
            continue;
        };
        let name = normalize_package_name(&caps["name"]);
        let Some(version) = targets.get(&name) else {
            continue;
        };
        declared.insert(name);
        if let Some(updated) = rewrite_requirement(dependency, version) {
            log::info!("latest-packages: {dependency} -> {updated}");
            updates.push((index, updated));
        }
    }
    for (index, updated) in updates {
        toml_manifest::set_array_item(array, index, updated.as_str());
        changed = true;
    }
    changed
}

fn rewrite_document(
    doc: &mut DocumentMut,
    targets: &BTreeMap<String, String>,
    declared: &mut BTreeSet<String>,
) -> bool {
    let mut changed = false;
    if let Some(array) = toml_manifest::dig_table_mut(doc, &["project"])
        .and_then(|table| table.get_mut("dependencies"))
        .and_then(|item| item.as_array_mut())
    {
        changed |= rewrite_array(array, targets, declared);
    }
    for path in [
        ["project", "optional-dependencies"].as_slice(),
        ["dependency-groups"].as_slice(),
    ] {
        if let Some(table) = toml_manifest::dig_table_mut(doc, path) {
            for (_, item) in table.iter_mut() {
                if let Some(array) = item.as_array_mut() {
                    changed |= rewrite_array(array, targets, declared);
                }
            }
        }
    }
    changed
}

/// 把名单里声明了、却压根没写进依赖的包补进 `[project].dependencies`。
///
/// 不补的话这条配置等于白写：包不在依赖里，既进不了 wheel metadata，也不会被
/// `uv lock` 解析。
fn add_missing(doc: &mut DocumentMut, missing: &BTreeMap<String, String>, path: &Path) -> bool {
    let own = toml_manifest::dig_nonempty(doc, &["project", "name"])
        .map(|name| normalize_package_name(&name))
        .unwrap_or_default();

    let Some(project) = toml_manifest::dig_table_mut(doc, &["project"]) else {
        // 编排仓库的根 pyproject 可能根本不是一个 PEP 621 包。凭空造出 [project]
        // 只会生成一份缺 name/version 的残缺元数据。
        log::warn!(
            "latest-packages: {} 没有 [project] 表, 无法补齐 {:?}",
            path.display(),
            missing.keys().collect::<Vec<_>>()
        );
        return false;
    };

    if project.get("dependencies").is_none() {
        project.insert("dependencies", toml_edit::Item::Value(Array::new().into()));
    }
    let Some(dependencies) = project
        .get_mut("dependencies")
        .and_then(|item| item.as_array_mut())
    else {
        log::warn!(
            "latest-packages: {} 的 [project].dependencies 不是数组, 无法补齐 {:?}",
            path.display(),
            missing.keys().collect::<Vec<_>>()
        );
        return false;
    };

    let mut changed = false;
    for (package, version) in missing {
        if !own.is_empty() && normalize_package_name(package) == own {
            // 自己依赖自己会让 uv 直接解析失败。编排仓库沿用整链名单时，本仓库
            // 自己的包名必然在名单里，这条不是边角情况。
            log::info!(
                "latest-packages: 跳过 {package}, 它就是 {} 自己的包",
                path.display()
            );
            continue;
        }
        let requirement = format!("{package}>={version}");
        log::info!(
            "latest-packages: {} 的 dependencies 里没有 {package}, 补上 {requirement}",
            path.display()
        );
        dependencies.push(requirement);
        changed = true;
    }
    changed
}

/// 把配置里声明的包，在各 pyproject.toml 中的版本下界抬到最新已发布版本。
///
/// 没有任何配置时直接返回，不发起任何网络请求——绝大多数仓库都走这条路径。
pub fn sync_latest_dependencies(repo: &Path, toml_paths: &[PathBuf]) -> Result<Vec<PathBuf>> {
    let packages = latest_packages(repo);
    if packages.is_empty() {
        return Ok(Vec::new());
    }

    let mut targets = BTreeMap::new();
    for package in &packages {
        let version = resolve_latest_version(package, repo)?;
        log::info!("latest-packages: 解析到 {package} 的最新版本 {version}");
        targets.insert(normalize_package_name(package), version);
    }

    // 先全部读进来再统一落盘：「有没有声明过」要看齐所有清单（包在子包里声明了就
    // 不该再往根上补一份），而补齐又要改根文档——边读边写会让根文件被写两次，
    // 第二次还是改在一份过期的文档上。
    let root_path = repo.join("pyproject.toml");
    let mut documents: Vec<(PathBuf, DocumentMut)> = Vec::new();
    let mut seen: BTreeSet<PathBuf> = BTreeSet::new();
    for path in toml_paths {
        let key = path.canonicalize().unwrap_or_else(|_| path.clone());
        if seen.insert(key) {
            documents.push((path.clone(), load_for_rewrite(path)?));
        }
    }
    let root_key = root_path
        .canonicalize()
        .unwrap_or_else(|_| root_path.clone());
    let root_index = documents
        .iter()
        .position(|(path, _)| path.canonicalize().unwrap_or_else(|_| path.clone()) == root_key);
    let root_index = match root_index {
        Some(index) => Some(index),
        None if root_path.is_file() => {
            documents.push((root_path.clone(), load_for_rewrite(&root_path)?));
            Some(documents.len() - 1)
        }
        None => None,
    };

    let mut declared = BTreeSet::new();
    let mut changed: Vec<bool> = Vec::with_capacity(documents.len());
    for (_, doc) in documents.iter_mut() {
        changed.push(rewrite_document(doc, &targets, &mut declared));
    }

    let missing: BTreeMap<String, String> = pyproject_latest_packages(repo)
        .into_iter()
        .filter(|package| !declared.contains(&normalize_package_name(package)))
        .filter_map(|package| {
            targets
                .get(&normalize_package_name(&package))
                .map(|version| (package, version.clone()))
        })
        .collect();
    if !missing.is_empty() {
        match root_index {
            Some(index) => {
                let (path, doc) = &mut documents[index];
                let path = path.clone();
                changed[index] |= add_missing(doc, &missing, &path);
            }
            None => log::warn!(
                "latest-packages: 找不到 {}, 无法补齐 {:?}",
                root_path.display(),
                missing.keys().collect::<Vec<_>>()
            ),
        }
    }

    let mut written = Vec::new();
    for (index, (path, doc)) in documents.iter().enumerate() {
        if changed[index] {
            toml_manifest::save(doc, path)?;
            written.push(path.clone());
        }
    }
    Ok(written)
}

fn load_for_rewrite(path: &Path) -> Result<DocumentMut> {
    toml_manifest::load(path).map_err(|e| {
        Error::latest_dependency(format!(
            "无法解析 {}, 拒绝带着过期的依赖下界继续发布: {e}",
            path.display()
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrites_lower_bound_keeping_upper_and_marker() {
        assert_eq!(
            rewrite_requirement("funflix[all]>=1.0,<2; python_version>=\"3.11\"", "1.9.0").unwrap(),
            "funflix[all]>=1.9.0,<2; python_version>=\"3.11\""
        );
    }

    #[test]
    fn adds_bound_when_none_declared() {
        assert_eq!(
            rewrite_requirement("funflix", "1.9.0").unwrap(),
            "funflix>=1.9.0"
        );
    }

    #[test]
    fn rewrites_every_lower_bound_operator() {
        for op in LOWER_BOUND_OPERATORS {
            let original = format!("funflix{op}1.0.0");
            assert_eq!(
                rewrite_requirement(&original, "1.9.0").unwrap(),
                format!("funflix{op}1.9.0"),
                "operator {op}"
            );
        }
    }

    #[test]
    fn keeps_exclusions_and_upper_bounds_untouched() {
        assert_eq!(
            rewrite_requirement("funflix<2.0", "1.9.0").unwrap(),
            "funflix>=1.9.0,<2.0"
        );
        assert_eq!(
            rewrite_requirement("funflix!=1.5,>=1.0", "1.9.0").unwrap(),
            "funflix!=1.5,>=1.9.0"
        );
        assert_eq!(
            rewrite_requirement("funflix===1.0", "1.9.0").unwrap(),
            "funflix>=1.9.0,===1.0"
        );
    }

    #[test]
    fn leaves_direct_references_alone() {
        assert_eq!(
            rewrite_requirement("funflix @ https://example.com/x.whl", "1.9.0"),
            None
        );
    }

    #[test]
    fn no_change_returns_none() {
        assert_eq!(rewrite_requirement("funflix>=1.9.0", "1.9.0"), None);
    }

    #[test]
    fn collapses_duplicate_lower_bounds() {
        assert_eq!(
            rewrite_requirement("funflix>=1.0,>=1.2", "1.9.0").unwrap(),
            "funflix>=1.9.0"
        );
    }

    #[test]
    fn handles_parenthesised_specifier() {
        assert_eq!(
            rewrite_requirement("funflix (>=1.0)", "1.9.0").unwrap(),
            "funflix>=1.9.0"
        );
    }

    #[test]
    fn reads_config_from_both_sources() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[tool.funbuild]\nlatest-packages = [\"funflix\", \"funauth\"]\n",
        )
        .unwrap();
        std::fs::create_dir_all(dir.path().join("scripts")).unwrap();
        std::fs::write(
            dir.path().join("scripts/funbuild.toml"),
            "version = \"1.0.0\"\npackages = [\"funflix\", \"funworker\"]\n",
        )
        .unwrap();

        assert_eq!(
            pyproject_latest_packages(dir.path()),
            vec!["funflix", "funauth"]
        );
        // 并集，去重
        assert_eq!(
            latest_packages(dir.path()),
            vec!["funflix", "funauth", "funworker"]
        );
    }

    #[test]
    fn no_config_means_no_work() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nname = \"demo\"\n",
        )
        .unwrap();
        let paths = vec![dir.path().join("pyproject.toml")];
        assert!(sync_latest_dependencies(dir.path(), &paths)
            .unwrap()
            .is_empty());
    }

    fn rewrite_only(doc: &mut DocumentMut, targets: &[(&str, &str)]) -> (bool, BTreeSet<String>) {
        let targets: BTreeMap<String, String> = targets
            .iter()
            .map(|(k, v)| (normalize_package_name(k), v.to_string()))
            .collect();
        let mut declared = BTreeSet::new();
        let changed = rewrite_document(doc, &targets, &mut declared);
        (changed, declared)
    }

    #[test]
    fn rewrites_all_dependency_tables_and_keeps_comments() {
        let mut doc: DocumentMut = r#"
[project]
name = "funflix-api"
dependencies = [
    # 这条注释必须留住
    "funflix>=1.0",
    "requests",
]

[project.optional-dependencies]
reader = ["funflix>=1.0"]

[dependency-groups]
dev = ["funflix>=1.0", { include-group = "test" }]
"#
        .parse()
        .unwrap();

        let (changed, declared) = rewrite_only(&mut doc, &[("funflix", "1.9.0")]);
        assert!(changed);
        assert!(declared.contains("funflix"));

        let rendered = doc.to_string();
        assert_eq!(rendered.matches("funflix>=1.9.0").count(), 3, "{rendered}");
        assert!(rendered.contains("# 这条注释必须留住"), "{rendered}");
        // 表项不受影响
        assert!(
            rendered.contains("{ include-group = \"test\" }"),
            "{rendered}"
        );
    }

    #[test]
    fn declared_includes_packages_that_need_no_rewrite() {
        let mut doc: DocumentMut = "[project]\ndependencies = [\"funflix @ https://x/y.whl\"]\n"
            .parse()
            .unwrap();
        let (changed, declared) = rewrite_only(&mut doc, &[("funflix", "1.9.0")]);
        assert!(!changed);
        assert!(
            declared.contains("funflix"),
            "直接引用也算声明过，不该再被补一份"
        );
    }

    #[test]
    fn matches_names_case_and_separator_insensitively() {
        let mut doc: DocumentMut = "[project]\ndependencies = [\"Funflix_API>=1.0\"]\n"
            .parse()
            .unwrap();
        let (changed, _) = rewrite_only(&mut doc, &[("funflix-api", "2.0.0")]);
        assert!(changed);
        assert!(doc.to_string().contains("Funflix_API>=2.0.0"));
    }

    #[test]
    fn adds_missing_dependency_but_never_itself() {
        let mut doc: DocumentMut = "[project]\nname = \"funflix\"\ndependencies = []\n"
            .parse()
            .unwrap();
        let missing: BTreeMap<String, String> = [
            ("funflix".to_string(), "1.9.0".to_string()),
            ("funauth".to_string(), "2.0.0".to_string()),
        ]
        .into();
        assert!(add_missing(&mut doc, &missing, Path::new("pyproject.toml")));
        let rendered = doc.to_string();
        assert!(rendered.contains("funauth>=2.0.0"), "{rendered}");
        assert!(
            !rendered.contains("funflix>="),
            "不能让包依赖自己: {rendered}"
        );
    }

    #[test]
    fn creates_dependencies_array_when_absent() {
        let mut doc: DocumentMut = "[project]\nname = \"demo\"\n".parse().unwrap();
        let missing: BTreeMap<String, String> =
            [("funflix".to_string(), "1.9.0".to_string())].into();
        assert!(add_missing(&mut doc, &missing, Path::new("pyproject.toml")));
        assert!(doc.to_string().contains("funflix>=1.9.0"));
    }

    #[test]
    fn refuses_to_invent_project_table() {
        let mut doc: DocumentMut = "[tool.funbuild]\nlatest-packages = [\"funflix\"]\n"
            .parse()
            .unwrap();
        let missing: BTreeMap<String, String> =
            [("funflix".to_string(), "1.9.0".to_string())].into();
        assert!(!add_missing(
            &mut doc,
            &missing,
            Path::new("pyproject.toml")
        ));
        assert!(!doc.to_string().contains("[project]"));
    }

    #[test]
    fn resolve_failure_explains_private_index() {
        let dir = tempfile::tempdir().unwrap();
        // 这个包名不可能存在于任何 index 上
        let error =
            resolve_latest_version("funbuild-definitely-not-a-real-package-xyz", dir.path())
                .unwrap_err();
        let text = error.to_string();
        assert!(matches!(error, Error::LatestDependency(_)));
        assert!(text.contains("拒绝带着过期的依赖下界继续发布"), "{text}");
        assert!(text.contains("[[tool.uv.index]]"), "{text}");
    }
}
