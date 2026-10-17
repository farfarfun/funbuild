//! package.json 的读写。
//!
//! serde_json 开了 preserve_order，键序和原文件一致；pretty 输出是 2 空格、
//! 非 ASCII 不转义，对齐 Python 的 `json.dump(indent=2, ensure_ascii=False)`。

use std::path::Path;

use serde_json::{Map, Value};

use crate::error::{self, Error, Result};

pub fn load(path: impl AsRef<Path>) -> Result<Value> {
    let path = path.as_ref();
    let raw = error::read_to_string(path)?;
    serde_json::from_str(&raw).map_err(|e| Error::manifest_parse("JSON", path, e))
}

/// 读成顶层对象；不是对象时报错。
pub fn load_object(path: impl AsRef<Path>) -> Result<Map<String, Value>> {
    let path = path.as_ref();
    match load(path)? {
        Value::Object(map) => Ok(map),
        _ => Err(Error::manifest_parse("JSON", path, "顶层不是对象")),
    }
}

pub fn save(value: &Map<String, Value>, path: impl AsRef<Path>) -> Result<()> {
    let mut rendered = serde_json::to_string_pretty(value)
        .map_err(|e| Error::invalid(format!("序列化 JSON 失败: {e}")))?;
    rendered.push('\n');
    error::write(path, rendered)
}

/// 非空字符串字段，顺带 trim。
pub fn str_field(map: &Map<String, Value>, key: &str) -> Option<String> {
    let value = map.get(key)?.as_str()?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_preserves_key_order_and_unicode() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("package.json");
        std::fs::write(
            &path,
            "{\n  \"name\": \"demo\",\n  \"version\": \"1.0.0\",\n  \"description\": \"中文说明\"\n}\n",
        )
        .unwrap();

        let mut pkg = load_object(&path).unwrap();
        pkg.insert("version".into(), Value::String("1.0.1".into()));
        save(&pkg, &path).unwrap();

        let rendered = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            rendered,
            "{\n  \"name\": \"demo\",\n  \"version\": \"1.0.1\",\n  \"description\": \"中文说明\"\n}\n"
        );
    }

    #[test]
    fn reports_path_on_parse_failure() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("package.json");
        std::fs::write(&path, "{not json").unwrap();
        let error = load_object(&path).unwrap_err();
        assert!(error.to_string().contains("package.json"), "{error}");
    }
}
