//! 提交信息的校验与生成。
//!
//! 校验**只告警不拦**：`<类型>: <描述>` 是约定不是门槛，判错的代价是丢掉写好的描述。

use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use regex::Regex;

use crate::error::Result;
use crate::{git, shell};

/// SPEC §10：格式 `<类型>: <做了什么>`，类型取这几个 ASCII 词。
pub const MESSAGE_TYPES: [&str; 6] = ["feat", "fix", "docs", "refactor", "test", "chore"];
pub const DEFAULT_MESSAGE: &str = "chore: 更新项目文件";
const AICOMMITS_TIMEOUT: Duration = Duration::from_secs(180);

pub fn message_hint() -> String {
    format!(
        "提交信息必须是 `<类型>: <描述>` 格式, 类型取 {}, 例如 {DEFAULT_MESSAGE:?}",
        MESSAGE_TYPES.join("/")
    )
}

/// 允许 conventional commits 的可选 scope（如 `fix(core): ...`），描述不限语种。
fn message_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(&format!(
            r"^(?:{})(?:\([^()\s]+\))?: \S.*$",
            MESSAGE_TYPES.join("|")
        ))
        .unwrap()
    })
}

/// 首行是否符合 `<类型>: <描述>`。
pub fn is_valid(message: &str) -> bool {
    match message.lines().next() {
        Some(subject) => message_re().is_match(subject),
        None => false,
    }
}

/// 不合约定时告警一句，不中止流程、不改写信息。
pub fn warn_if_invalid(message: Option<&str>) {
    if let Some(message) = message {
        if !is_valid(message) {
            log::warn!("{}; 收到 {message:?}, 按原样提交", message_hint());
        }
    }
}

fn warn_if_unconventional(message: &str) {
    if !message.is_empty() && !is_valid(message) {
        log::warn!(
            "提交信息不符合约定 ({}), 按原样保留: {message:?}",
            message_hint()
        );
    }
}

// 推理模型会把思维链包在 <think> 里，aicommits 不剥离就拿去提交，于是 git 历史里
// 出现整条正文只有 "<think>" 的提交。
//
// 三个标签各写一遍开闭：Python 原版用 `(think|thinking|reasoning)...\1` 的反向引用，
// 而 Rust 的 regex 不支持反向引用。
fn think_block_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?is)<\s*think\s*>.*?<\s*/\s*think\s*>|<\s*thinking\s*>.*?<\s*/\s*thinking\s*>|<\s*reasoning\s*>.*?<\s*/\s*reasoning\s*>",
        )
        .unwrap()
    })
}

fn think_close_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)<\s*/\s*(?:think|thinking|reasoning)\s*>").unwrap())
}

fn think_open_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)<\s*(?:think|thinking|reasoning)\s*>").unwrap())
}

fn fence_line_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^\s*```.*$").unwrap())
}

/// 剥掉思维链标记与 markdown 围栏。
///
/// 结论通常在 `</think>` 之后；只剩未闭合的 `<think>` 说明输出被截断，整段作废。
pub fn sanitize(message: &str) -> String {
    let stripped = think_block_re().replace_all(message, "");
    let mut text: &str = &stripped;

    // 落单的闭合标记：结论在它之后
    if let Some(last) = think_close_re().find_iter(text).last() {
        text = &text[last.end()..];
    }
    // 落单的开启标记：其后是被截断的思维链，整段丢弃
    if let Some(open) = think_open_re().find(text) {
        text = &text[..open.start()];
    }

    let without_fences = fence_line_re().replace_all(text, "");
    without_fences
        .trim()
        .lines()
        .map(|line| line.trim_end())
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

// aicommits 的默认 type=plain 只输出纯描述，必然缺 `<类型>:` 前缀；而它的
// conventional 模式又会产出表外的 perf/style/ci。格式由 funbuild 交代清楚，
// 类型仍由模型按 diff 判断。不传 --locale：语种听用户 ~/.aicommits 的。
fn aicommits_command() -> String {
    let prompt = format!(
        "The subject line must start with exactly one of these types followed by \": \" - {}. Never use any other type.",
        MESSAGE_TYPES.join(", ")
    );
    shell::join([
        "aicommits",
        "--yes",
        "--type",
        "conventional",
        "--prompt",
        &prompt,
    ])
}

/// 调用失败多是配置级的（模型下线、密钥失效、网关不通），本进程内记下就不再重试。
fn failure() -> &'static Mutex<Option<String>> {
    static FAILURE: OnceLock<Mutex<Option<String>>> = OnceLock::new();
    FAILURE.get_or_init(|| Mutex::new(None))
}

fn available() -> bool {
    static AVAILABLE: OnceLock<bool> = OnceLock::new();
    *AVAILABLE.get_or_init(|| {
        let found = shell::capture("command -v aicommits", None, Some(Duration::from_secs(10)))
            .map(|output| output.ok())
            .unwrap_or(false);
        if !found {
            log::warn!("aicommits not found, fallback to default commit message");
        }
        found
    })
}

/// 清掉失败记录，仅测试用。
#[cfg(test)]
pub(crate) fn reset_failure() {
    *failure().lock().unwrap() = None;
}

fn record_failure(reason: impl Into<String>) {
    *failure().lock().unwrap() = Some(reason.into());
}

fn already_failed() -> bool {
    failure().lock().unwrap().is_some()
}

// aicommits 的报错形如 `✖ <原因>`，后面跟着整段 node 栈，只有带原因那行有用。
fn mark_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)^[\s✔✖⚠×x]+").unwrap())
}

fn failure_reason(stderr: &str, stdout: &str, code: i32) -> String {
    for raw in stderr.lines() {
        let line = mark_re().replace(raw, "").trim().to_string();
        if !line.is_empty() && !line.starts_with("at ") && !line.starts_with("https://") {
            return line;
        }
    }
    format!("exit={code}, stdout={:?}", stdout.trim())
}

/// 让 aicommits 依据暂存内容生成信息并提交，成功返回 true。
///
/// 只在调用方没指定信息时才该走这条路：aicommits 无视外部传入的信息。它的
/// `--yes` 只在 TTY 下才真提交，经 shell 调用时只把信息打到 stdout 就退出
/// （退出码仍是 0，暂存原地不动），所以这里捕获输出，没提交就拿这条信息自己提交。
pub fn aicommits_commit(cwd: Option<&Path>, fallback: &str) -> Result<bool> {
    if !git::has_staged_changes(cwd)? {
        log::warn!("No staged changes");
        return Ok(false);
    }
    if !available() || already_failed() {
        return Ok(false);
    }

    let command = aicommits_command();
    let output = match shell::capture(&command, cwd, Some(AICOMMITS_TIMEOUT)) {
        Ok(output) => output,
        Err(crate::Error::Timeout { timeout_secs, .. }) => {
            record_failure(format!("{timeout_secs}s 未返回"));
            log::warn!("aicommits {timeout_secs}s 未返回, 本次改用兜底信息 {fallback:?}");
            return Ok(false);
        }
        Err(error) => {
            record_failure(error.to_string());
            log::error!("aicommits commit failed: {error}");
            return Ok(false);
        }
    };

    // 它自己已经提交了（TTY 场景）：只清理信息里的污染
    if !git::has_staged_changes(cwd)? {
        repair_generated_message(cwd, fallback)?;
        return Ok(true);
    }

    let message = sanitize(&output.stdout);
    if message.is_empty() {
        let reason = failure_reason(&output.stderr, &output.stdout, output.code);
        record_failure(reason.clone());
        log::warn!("aicommits 没能生成信息, 本次改用兜底信息 {fallback:?}; 原因: {reason}");
        log::warn!("多为 ~/.aicommits 里的模型/密钥/地址失效, 可用 `aicommits config get OPENAI_MODEL` 核对");
        return Ok(false);
    }
    warn_if_unconventional(&message);
    shell::run_checked(&[shell::join(["git", "commit", "-m", &message])], cwd)?;
    Ok(true)
}

/// 只清掉思维链/围栏污染；格式不合约定只告警，不改写。
fn repair_generated_message(cwd: Option<&Path>, fallback: &str) -> Result<()> {
    let original = git::last_commit_message(cwd);
    let cleaned = sanitize(&original);
    if cleaned == original.trim() {
        warn_if_unconventional(&cleaned);
        return Ok(());
    }
    let replacement = if cleaned.is_empty() {
        fallback
    } else {
        &cleaned
    };
    log::warn!("aicommits 生成的信息含思维链/围栏标记, 已清理为: {replacement:?}");
    shell::run_checked(
        &[shell::join(["git", "commit", "--amend", "-m", replacement])],
        cwd,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_conventional_subjects() {
        assert!(is_valid("fix: 修复版本解析"));
        assert!(is_valid("feat(core): add thing"));
        assert!(is_valid("chore: 更新项目文件"));
        // 多行信息只看首行
        assert!(is_valid("fix: 标题\n\n正文"));
    }

    #[test]
    fn rejects_unconventional_subjects() {
        assert!(!is_valid(""));
        assert!(!is_valid("修复了一个问题"));
        assert!(!is_valid("perf: 不在类型表里"));
        assert!(!is_valid("fix:缺空格"));
        assert!(!is_valid("fix: "));
        assert!(!is_valid("fix(有 空格): x"));
    }

    #[test]
    fn strips_complete_think_block() {
        assert_eq!(
            sanitize("<think>想了很久</think>fix: 真结论"),
            "fix: 真结论"
        );
        assert_eq!(sanitize("<thinking>a</thinking>fix: b"), "fix: b");
        assert_eq!(sanitize("<reasoning>a</reasoning>fix: b"), "fix: b");
    }

    #[test]
    fn handles_multiline_and_mixed_case_tags() {
        assert_eq!(
            sanitize("<THINK>\n多行\n思维链\n</THINK>\nfix: 结论"),
            "fix: 结论"
        );
        assert_eq!(sanitize("< think >a</ think >fix: b"), "fix: b");
    }

    #[test]
    fn keeps_text_after_dangling_close_tag() {
        assert_eq!(sanitize("前面是思维链</think>\nfix: 结论"), "fix: 结论");
    }

    #[test]
    fn drops_truncated_think_tail() {
        // 只剩未闭合的开启标记：其后是被截断的思维链
        assert_eq!(sanitize("fix: 结论\n<think>还没说完"), "fix: 结论");
        assert_eq!(sanitize("<think>整条都是思维链"), "");
    }

    #[test]
    fn strips_markdown_fences() {
        assert_eq!(sanitize("```\nfix: 结论\n```"), "fix: 结论");
        assert_eq!(sanitize("```text\nfix: 结论\n```"), "fix: 结论");
    }

    #[test]
    fn trims_trailing_whitespace_per_line() {
        assert_eq!(
            sanitize("  fix: 标题   \n\n  正文  \n"),
            "fix: 标题\n\n  正文"
        );
    }

    #[test]
    fn failure_reason_prefers_marked_stderr_line() {
        let reason = failure_reason("✖ model not found\n    at Object.<anonymous>\n", "", 1);
        assert_eq!(reason, "model not found");
    }

    #[test]
    fn failure_reason_skips_stack_and_urls() {
        let reason = failure_reason(
            "at foo.js:1\nhttps://example.com/docs\n⚠ quota exceeded\n",
            "",
            1,
        );
        assert_eq!(reason, "quota exceeded");
    }

    #[test]
    fn failure_reason_falls_back_to_exit_code() {
        assert_eq!(
            failure_reason("", "some stdout", 7),
            "exit=7, stdout=\"some stdout\""
        );
    }

    #[test]
    fn command_carries_type_table_and_prompt() {
        let command = aicommits_command();
        assert!(command.contains("--type conventional"));
        assert!(command.contains("feat, fix, docs, refactor, test, chore"));
        assert!(!command.contains("--locale"));
    }

    #[test]
    fn no_staged_changes_means_no_call() {
        let dir = tempfile::tempdir().unwrap();
        git::testing::init_repo(dir.path());
        reset_failure();
        assert!(!aicommits_commit(Some(dir.path()), DEFAULT_MESSAGE).unwrap());
    }
}
