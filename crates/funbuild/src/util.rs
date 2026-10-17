use std::sync::OnceLock;

use regex::Regex;

/// PEP 503 归一化：大小写、`_`/`.`/`-` 的写法差异不应影响是否命中配置。
///
/// npm/pub 的包名惯例本就全小写，借同一套规则统一比对。
pub fn normalize_package_name(name: &str) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"[-_.]+").unwrap());
    re.replace_all(name.trim().to_lowercase().as_str(), "-")
        .into_owned()
}

/// cleanDirs 这类用户配置会被拼进 `rm -rf <值>` 交给 shell，必须先过滤。
///
/// 允许 glob（默认清理项本来就有 `extbuild/*/dist`），但不允许任何能改变命令结构
/// 或把删除范围带出仓库的字符。
const SHELL_METACHARS: [char; 16] = [
    ';', '&', '|', '$', '`', '(', ')', '<', '>', '\n', '\r', '\t', '"', '\'', '\\', ' ',
];

/// 校验 `funbuild.cleanDirs` 里的一项，通过则返回可直接拼进 `rm -rf` 的字符串。
pub fn safe_clean_dir(value: &str, source: &str) -> Option<String> {
    let item = value.trim();
    if item.is_empty() {
        return None;
    }
    if std::path::Path::new(item).is_absolute() || item.starts_with('~') {
        log::warn!("忽略 {source} 的 cleanDirs 项 {item:?}: 只允许仓库内的相对路径");
        return None;
    }
    if item.split(['/', '\\']).any(|part| part == "..") {
        log::warn!("忽略 {source} 的 cleanDirs 项 {item:?}: 不允许 `..` 跳出仓库");
        return None;
    }
    if item.chars().any(|c| SHELL_METACHARS.contains(&c)) {
        log::warn!("忽略 {source} 的 cleanDirs 项 {item:?}: 含 shell 元字符或空白");
        return None;
    }
    Some(item.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_separators_and_case() {
        assert_eq!(normalize_package_name("Funflix_API"), "funflix-api");
        assert_eq!(normalize_package_name("fun.build"), "fun-build");
        assert_eq!(normalize_package_name("  funget--core  "), "funget-core");
    }

    #[test]
    fn accepts_relative_paths_and_globs() {
        assert_eq!(safe_clean_dir("dist", "test").as_deref(), Some("dist"));
        assert_eq!(
            safe_clean_dir("extbuild/*/dist", "test").as_deref(),
            Some("extbuild/*/dist")
        );
        assert_eq!(
            safe_clean_dir("  build  ", "test").as_deref(),
            Some("build")
        );
    }

    #[test]
    fn rejects_paths_that_escape_the_repository() {
        assert_eq!(safe_clean_dir("/", "test"), None);
        assert_eq!(safe_clean_dir("/etc", "test"), None);
        assert_eq!(safe_clean_dir("~/.ssh", "test"), None);
        assert_eq!(safe_clean_dir("../..", "test"), None);
        assert_eq!(safe_clean_dir("a/../../b", "test"), None);
    }

    #[test]
    fn rejects_shell_injection() {
        assert_eq!(safe_clean_dir("dist; rm -rf /", "test"), None);
        assert_eq!(safe_clean_dir("dist build", "test"), None);
        assert_eq!(safe_clean_dir("$(whoami)", "test"), None);
        assert_eq!(safe_clean_dir("`id`", "test"), None);
        assert_eq!(safe_clean_dir("a|b", "test"), None);
        assert_eq!(safe_clean_dir("", "test"), None);
    }
}
