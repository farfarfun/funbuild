//! TOML 的读、改、写。
//!
//! 用 toml_edit 而不是 toml：upgrade 每次只改一个版本号，格式化器重排整个
//! pyproject.toml 既污染 diff 也会删掉用户写的注释。

use std::path::Path;
use std::sync::OnceLock;

use toml_edit::{DocumentMut, Item, Table, TableLike, Value};

use crate::error::{self, Error, Result};

pub fn load(path: impl AsRef<Path>) -> Result<DocumentMut> {
    let path = path.as_ref();
    let raw = error::read_to_string(path)?;
    raw.parse::<DocumentMut>()
        .map_err(|e| Error::manifest_parse("TOML", path, e))
}

pub fn save(doc: &DocumentMut, path: impl AsRef<Path>) -> Result<()> {
    error::write(path, doc.to_string())
}

/// 逐层下钻，任一层缺失返回 None。对应 Python 的 `deep_get`。
pub fn dig<'a>(doc: &'a DocumentMut, path: &[&str]) -> Option<&'a Item> {
    let (first, rest) = path.split_first()?;
    let mut current = doc.as_table().get(first)?;
    for key in rest {
        current = current.as_table_like()?.get(key)?;
    }
    Some(current)
}

pub fn dig_str<'a>(doc: &'a DocumentMut, path: &[&str]) -> Option<&'a str> {
    dig(doc, path)?.as_str()
}

/// 下钻取非空字符串，顺带 trim。
pub fn dig_nonempty(doc: &DocumentMut, path: &[&str]) -> Option<String> {
    let value = dig_str(doc, path)?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

pub fn dig_bool(doc: &DocumentMut, path: &[&str]) -> Option<bool> {
    dig(doc, path)?.as_bool()
}

pub fn dig_table<'a>(doc: &'a DocumentMut, path: &[&str]) -> Option<&'a dyn TableLike> {
    dig(doc, path)?.as_table_like()
}

/// 下钻取字符串数组，去空白后丢掉空项；不是数组时返回空。
pub fn dig_string_list(doc: &DocumentMut, path: &[&str]) -> Vec<String> {
    let Some(array) = dig(doc, path).and_then(|item| item.as_array()) else {
        return Vec::new();
    };
    array
        .iter()
        .filter_map(|v| v.as_str())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

/// 同 [`dig_table`] 但可改；任一层缺失或不是表时返回 None。
pub fn dig_table_mut<'a>(doc: &'a mut DocumentMut, path: &[&str]) -> Option<&'a mut dyn TableLike> {
    let mut current: &mut dyn TableLike = doc.as_table_mut();
    for key in path {
        current = current.get_mut(key)?.as_table_like_mut()?;
    }
    Some(current)
}

pub fn has_table(doc: &DocumentMut, path: &[&str]) -> bool {
    dig(doc, path).is_some_and(|item| item.as_table_like().is_some())
}

/// 缺失的层按需创建，返回最后一层的可变引用。对应 Python 的 `deep_create` 的前半。
pub fn ensure_table<'a>(doc: &'a mut DocumentMut, path: &[&str]) -> Result<&'a mut Table> {
    let mut current = doc.as_table_mut();
    for key in path {
        let entry = current
            .entry(key)
            .or_insert_with(|| Item::Table(Table::new()));
        current = entry
            .as_table_mut()
            .ok_or_else(|| Error::invalid(format!("[{}] 不是表，无法写入", path.join("."))))?;
    }
    Ok(current)
}

/// 改写一个标量值但保留它原有的行内注释与空白。
///
/// 直接 `table[key] = value(..)` 会连 decor 一起换掉，于是 `version = "1" # 注释`
/// 那条注释在 upgrade 之后就没了。
pub fn set_value(table: &mut dyn TableLike, key: &str, new: impl Into<Value>) {
    let new = new.into();
    match table.get_mut(key).and_then(|item| item.as_value_mut()) {
        Some(slot) => {
            let decor = slot.decor().clone();
            *slot = new;
            *slot.decor_mut() = decor;
        }
        None => {
            table.insert(key, Item::Value(new));
        }
    }
}

/// 同 [`set_value`]，用于数组里的某一项。
pub fn set_array_item(array: &mut toml_edit::Array, index: usize, new: impl Into<Value>) {
    let new = new.into();
    if let Some(slot) = array.get_mut(index) {
        let decor = slot.decor().clone();
        *slot = new;
        *slot.decor_mut() = decor;
    }
}

/// 写成 `[[<key>]]` 表数组（tomlkit 对 list-of-dict 赋值也是这个形状）。
///
/// 整表重建，但把原有表头的 decor 搬过来——否则写在 `[[project.authors]]` 上方的
/// 注释和空行会被每次发版吃掉一次。
pub fn set_table_array(table: &mut Table, key: &str, rows: &[Vec<(&str, String)>]) {
    let previous: Vec<toml_edit::Decor> = table
        .get(key)
        .and_then(|item| item.as_array_of_tables())
        .map(|array| array.iter().map(|entry| entry.decor().clone()).collect())
        .unwrap_or_default();

    let mut array = toml_edit::ArrayOfTables::new();
    for (index, row) in rows.iter().enumerate() {
        let mut entry = Table::new();
        for (name, value) in row {
            entry.insert(name, Item::Value(value.as_str().into()));
        }
        if let Some(decor) = previous.get(index) {
            *entry.decor_mut() = decor.clone();
        }
        array.push(entry);
    }
    table.insert(key, Item::ArrayOfTables(array));
}

/// 写成 `[<key>]` 子表，同样保留原有表头的 decor。
pub fn set_subtable(table: &mut Table, key: &str, entries: &[(&str, String)]) {
    let previous = table
        .get(key)
        .and_then(|item| item.as_table())
        .map(|sub| sub.decor().clone());

    let mut sub = Table::new();
    for (name, value) in entries {
        sub.insert(name, Item::Value(value.as_str().into()));
    }
    if let Some(decor) = previous {
        *sub.decor_mut() = decor;
    }
    table.insert(key, Item::Table(sub));
}

pub fn string_array<I, S>(items: I) -> toml_edit::Array
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut array = toml_edit::Array::new();
    for item in items {
        array.push(item.as_ref());
    }
    array
}

/// 只认行首（允许缩进）的 version 键，避开依赖表里的 `foo = { version = "1" }`。
pub(crate) fn version_line_re() -> &'static regex::Regex {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(
            r#"(?m)^(?P<prefix>\s*version\s*=\s*")(?P<version>[^"]*)(?P<suffix>")\s*$"#,
        )
        .unwrap()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
# 顶部注释
[project]
name = "demo"
version = "1.0.0"  # 行内注释
dependencies = ["funflix>=1.0", "requests"]

[tool.funbuild]
latest-packages = ["farlog", "funshell"]

[tool.uv]
package = false
"#;

    fn doc() -> DocumentMut {
        SAMPLE.parse().unwrap()
    }

    #[test]
    fn digs_nested_values() {
        let d = doc();
        assert_eq!(dig_str(&d, &["project", "name"]), Some("demo"));
        assert_eq!(dig_bool(&d, &["tool", "uv", "package"]), Some(false));
        assert_eq!(dig_str(&d, &["project", "missing"]), None);
        assert_eq!(dig_str(&d, &["nope", "deeper"]), None);
    }

    #[test]
    fn digs_string_lists() {
        let d = doc();
        assert_eq!(
            dig_string_list(&d, &["tool", "funbuild", "latest-packages"]),
            vec!["farlog", "funshell"]
        );
        assert!(dig_string_list(&d, &["tool", "funbuild", "missing"]).is_empty());
    }

    #[test]
    fn set_value_keeps_inline_comment() {
        let mut d = doc();
        let project = dig_table_mut(&mut d, &["project"]).unwrap();
        set_value(project, "version", "1.0.1");
        let rendered = d.to_string();
        assert!(
            rendered.contains(r#"version = "1.0.1"  # 行内注释"#),
            "{rendered}"
        );
        assert!(rendered.contains("# 顶部注释"));
    }

    #[test]
    fn ensure_table_creates_missing_levels() {
        let mut d = doc();
        let table = ensure_table(&mut d, &["tool", "ruff", "lint"]).unwrap();
        set_value(table, "line-length", 120i64);
        assert_eq!(
            dig(&d, &["tool", "ruff", "lint", "line-length"])
                .unwrap()
                .as_integer(),
            Some(120)
        );
        // 既有内容不受影响
        assert_eq!(dig_str(&d, &["project", "name"]), Some("demo"));
    }

    #[test]
    fn set_array_item_rewrites_in_place() {
        let mut d = doc();
        let array = dig_table_mut(&mut d, &["project"])
            .unwrap()
            .get_mut("dependencies")
            .unwrap()
            .as_array_mut()
            .unwrap();
        set_array_item(array, 0, "funflix>=1.9.0");
        assert!(d.to_string().contains(r#"["funflix>=1.9.0", "requests"]"#));
    }
}
