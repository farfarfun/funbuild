//! pubspec.yaml 的读与改。
//!
//! 版本号用正则换那一行而不是重写 YAML：`+buildNumber` 后缀、引号风格和行内
//! 注释都得原样留着，整份 dump 回去会把它们冲掉。

use std::path::Path;
use std::sync::OnceLock;

use regex::Regex;
use serde_norway::Value;

use crate::error::{self, Error, Result};

/// 形如 `version: 1.0.0+42`，可能带引号与行内注释。
fn version_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^(version:\s*)(\S+)(.*)$").unwrap())
}

pub fn load(path: impl AsRef<Path>) -> Result<Value> {
    let path = path.as_ref();
    let raw = error::read_to_string(path)?;
    serde_norway::from_str(&raw).map_err(|e| Error::manifest_parse("YAML", path, e))
}

pub fn has_version_line(raw: &str) -> bool {
    version_re().is_match(raw)
}

/// 替换第一条 version 行，保留引号风格与行内注释。
///
/// `compute` 收到的是去掉引号的原值，返回同样不带引号的新值。
pub fn replace_version_line(raw: &str, compute: impl Fn(&str) -> String) -> (String, usize) {
    let re = version_re();
    let Some(caps) = re.captures(raw) else {
        return (raw.to_string(), 0);
    };
    let whole = caps.get(0).expect("group 0");
    let prefix = caps.get(1).map_or("", |m| m.as_str());
    let token = caps.get(2).map_or("", |m| m.as_str());
    let rest = caps.get(3).map_or("", |m| m.as_str());

    let mut quote = "";
    let mut inner = token;
    let bytes = token.as_bytes();
    if token.len() >= 2
        && (bytes[0] == b'"' || bytes[0] == b'\'')
        && bytes[0] == bytes[token.len() - 1]
    {
        quote = &token[..1];
        inner = &token[1..token.len() - 1];
    }

    let mut out = String::with_capacity(raw.len());
    out.push_str(&raw[..whole.start()]);
    out.push_str(prefix);
    out.push_str(quote);
    out.push_str(&compute(inner));
    out.push_str(quote);
    out.push_str(rest);
    out.push_str(&raw[whole.end()..]);
    (out, 1)
}

/// 顶层字段里的非空字符串。
pub fn str_field(data: &Value, key: &str) -> Option<String> {
    let value = data.get(key)?.as_str()?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

/// `dependencies` / `dev_dependencies` 下的包名，排除 flutter SDK 自带的两个。
pub fn dependency_names(data: &Value) -> Vec<String> {
    let mut names = Vec::new();
    for field in ["dependencies", "dev_dependencies"] {
        let Some(Value::Mapping(map)) = data.get(field) else {
            continue;
        };
        for key in map.keys() {
            if let Some(name) = key.as_str() {
                if name != "flutter" && name != "flutter_test" {
                    names.push(name.to_string());
                }
            }
        }
    }
    names
}

/// 是否显式依赖 Flutter SDK（`dependencies.flutter.sdk: flutter`）。
pub fn uses_flutter_sdk(data: &Value) -> bool {
    data.get("dependencies")
        .and_then(|deps| deps.get("flutter"))
        .and_then(|flutter| flutter.get("sdk"))
        .and_then(|sdk| sdk.as_str())
        == Some("flutter")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replaces_bare_version() {
        let (out, count) = replace_version_line("name: demo\nversion: 1.0.0\n", |_| "2.0.0".into());
        assert_eq!(count, 1);
        assert_eq!(out, "name: demo\nversion: 2.0.0\n");
    }

    #[test]
    fn keeps_quotes_and_trailing_comment() {
        let (out, _) = replace_version_line("version: \"1.0.0+3\"  # 商店构建号\n", |inner| {
            assert_eq!(inner, "1.0.0+3");
            "1.0.1+4".into()
        });
        assert_eq!(out, "version: \"1.0.1+4\"  # 商店构建号\n");
    }

    #[test]
    fn keeps_build_number_when_only_main_changes() {
        let (out, _) =
            replace_version_line("version: 1.0.0+42\n", |inner| match inner.split_once('+') {
                Some((_, build)) => format!("2.0.0+{build}"),
                None => "2.0.0".into(),
            });
        assert_eq!(out, "version: 2.0.0+42\n");
    }

    #[test]
    fn only_first_occurrence() {
        let (out, count) =
            replace_version_line("version: 1.0.0\nversion: 9.9.9\n", |_| "2.0.0".into());
        assert_eq!(count, 1);
        assert_eq!(out, "version: 2.0.0\nversion: 9.9.9\n");
    }

    #[test]
    fn reports_missing_version_line() {
        let (out, count) = replace_version_line("name: demo\n", |_| "2.0.0".into());
        assert_eq!(count, 0);
        assert_eq!(out, "name: demo\n");
        assert!(!has_version_line("name: demo\n"));
    }

    #[test]
    fn reads_flutter_sdk_and_dependency_names() {
        let raw = "name: demo\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n  funtrack: ^1.0.0\ndev_dependencies:\n  flutter_test:\n    sdk: flutter\n  build_runner: ^2.0.0\n";
        let data: Value = serde_norway::from_str(raw).unwrap();
        assert!(uses_flutter_sdk(&data));
        assert_eq!(str_field(&data, "name").as_deref(), Some("demo"));
        assert_eq!(dependency_names(&data), vec!["funtrack", "build_runner"]);
    }

    #[test]
    fn plain_dart_package_is_not_flutter() {
        let data: Value =
            serde_norway::from_str("name: demo\ndependencies:\n  path: ^1.0.0\n").unwrap();
        assert!(!uses_flutter_sdk(&data));
    }
}
