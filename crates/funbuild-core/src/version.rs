use std::sync::OnceLock;

use regex::Regex;

use crate::error::{Error, Result};

/// 形如 1、1.6、1.6.54、v1.6.54rc1 —— 取前导数字段，其余作为后缀。
fn version_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*?)\s*$").unwrap())
}

/// 自增的进位基数：patch 满 128 向 minor 进位。
pub const STEP: u64 = 128;

/// 解析成 [major, minor, patch] 与剩余后缀（如 "rc1"）；缺失的段补 0。
pub fn parse_version(version: &str) -> Result<([u64; 3], String)> {
    let caps = version_re()
        .captures(version)
        .ok_or_else(|| Error::invalid(format!("cannot parse version: {version:?}")))?;
    let mut numbers = [0u64; 3];
    for (index, slot) in numbers.iter_mut().enumerate() {
        *slot = match caps.get(index + 1) {
            Some(m) => m
                .as_str()
                .parse()
                .map_err(|_| Error::invalid(format!("cannot parse version: {version:?}")))?,
            None => 0,
        };
    }
    let suffix = caps.get(4).map(|m| m.as_str()).unwrap_or("").to_string();
    Ok((numbers, suffix))
}

/// 按 STEP 进制自增一位 patch。后缀会被丢掉。
pub fn next_version(current: Option<&str>) -> Result<String> {
    let version = current.filter(|v| !v.is_empty()).unwrap_or("0.0.1");
    let (parts, suffix) = parse_version(version)?;
    if !suffix.is_empty() {
        log::warn!("version {version:?} has suffix {suffix:?}, dropped when upgrading");
    }
    let total = parts[0] * STEP * STEP + parts[1] * STEP + parts[2] + 1;
    Ok(format!(
        "{}.{}.{}",
        total / (STEP * STEP),
        total / STEP % STEP,
        total % STEP
    ))
}

/// 剥掉前导的 v/V，对应 Python 的 `lstrip("vV")`。
pub fn strip_v_prefix(version: &str) -> &str {
    version.trim_start_matches(['v', 'V'])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_partial_versions() {
        assert_eq!(parse_version("1").unwrap(), ([1, 0, 0], String::new()));
        assert_eq!(parse_version("1.0").unwrap(), ([1, 0, 0], String::new()));
        assert_eq!(
            parse_version("1.6.54").unwrap(),
            ([1, 6, 54], String::new())
        );
    }

    #[test]
    fn keeps_suffix_and_tolerates_v_prefix_and_space() {
        assert_eq!(
            parse_version("v1.6.54rc1").unwrap(),
            ([1, 6, 54], "rc1".to_string())
        );
        assert_eq!(
            parse_version("  1.2.3  ").unwrap(),
            ([1, 2, 3], String::new())
        );
    }

    #[test]
    fn rejects_unparsable() {
        assert!(parse_version("").is_err());
        assert!(parse_version("abc").is_err());
        assert!(parse_version("1.2.3\nrc1").is_err());
    }

    #[test]
    fn increments_patch() {
        assert_eq!(next_version(Some("1.6.54")).unwrap(), "1.6.55");
        assert_eq!(next_version(None).unwrap(), "0.0.2");
    }

    #[test]
    fn carries_at_step() {
        assert_eq!(next_version(Some("1.6.127")).unwrap(), "1.7.0");
        assert_eq!(next_version(Some("1.127.127")).unwrap(), "2.0.0");
    }

    #[test]
    fn drops_suffix_when_upgrading() {
        assert_eq!(next_version(Some("1.6.54rc1")).unwrap(), "1.6.55");
    }

    #[test]
    fn strips_v_prefix() {
        assert_eq!(strip_v_prefix("v1.2.3"), "1.2.3");
        assert_eq!(strip_v_prefix("V1.2.3"), "1.2.3");
        assert_eq!(strip_v_prefix("1.2.3"), "1.2.3");
    }
}
