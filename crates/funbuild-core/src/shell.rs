//! 命令执行层。
//!
//! 一律走 `sh -c`：命令串里有 glob、子 shell、命令替换和 `export PATH=`，
//! 换成 `Command::args` 会让这些全部失效。

use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use wait_timeout::ChildExt;

use crate::error::{Error, Result};

#[derive(Debug)]
pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.code == 0
    }

    pub fn trimmed(&self) -> &str {
        self.stdout.trim()
    }
}

/// shell 元字符转义，对应 Python 的 `shlex.quote`。
pub fn quote(value: &str) -> String {
    shell_escape::unix::escape(value.into()).into_owned()
}

/// 对应 Python 的 `shlex.join`。
pub fn join<I, S>(parts: I) -> String
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    parts
        .into_iter()
        .map(|part| quote(part.as_ref()))
        .collect::<Vec<_>>()
        .join(" ")
}

fn build(command: &str, cwd: Option<&Path>) -> Command {
    let mut cmd = Command::new("sh");
    cmd.arg("-c").arg(command);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    cmd
}

/// 执行命令，stdout/stderr 直通终端，返回退出码。
pub fn status(command: &str, cwd: Option<&Path>) -> Result<i32> {
    log::debug!("$ {command}");
    let status = build(command, cwd).status()?;
    Ok(status.code().unwrap_or(-1))
}

/// 用 ` && ` 串成一条交给同一个 shell 进程，任一条失败即报错。
///
/// 必须是同一个进程：FlutterBuild 的 `export PATH=` 靠它对后续命令生效。
pub fn run_checked(commands: &[String], cwd: Option<&Path>) -> Result<()> {
    if commands.is_empty() {
        return Ok(());
    }
    let joined = commands.join(" && ");
    let code = status(&joined, cwd)?;
    if code != 0 {
        return Err(Error::Shell {
            command: joined,
            code: Some(code),
        });
    }
    Ok(())
}

/// 捕获 stdout 与 stderr。
///
/// 两个管道各起一个线程读：只用 `wait_timeout` 而不并发读，管道写满时子进程会
/// 阻塞在 write 上，父子互等。
pub fn capture(command: &str, cwd: Option<&Path>, timeout: Option<Duration>) -> Result<Output> {
    log::debug!("$ {command}");
    let mut child = build(command, cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let mut out_pipe = child.stdout.take().expect("stdout piped");
    let mut err_pipe = child.stderr.take().expect("stderr piped");
    let out_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = out_pipe.read_to_end(&mut buf);
        buf
    });
    let err_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = err_pipe.read_to_end(&mut buf);
        buf
    });

    let status = match timeout {
        Some(limit) => match child.wait_timeout(limit)? {
            Some(status) => status,
            None => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(Error::Timeout {
                    command: command.to_string(),
                    timeout_secs: limit.as_secs(),
                });
            }
        },
        None => child.wait()?,
    };

    let stdout = out_reader.join().unwrap_or_default();
    let stderr = err_reader.join().unwrap_or_default();
    Ok(Output {
        code: status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

/// 只要 stdout，失败与超时都折成空串 —— 对应 funshell 在出错时返回空/错误串的行为，
/// 调用方据此走各自的回退分支。
pub fn capture_stdout(command: &str, cwd: Option<&Path>, timeout: Option<Duration>) -> String {
    match capture(command, cwd, timeout) {
        Ok(output) => output.stdout,
        Err(error) => {
            log::debug!("capture failed: {command}: {error}");
            String::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quote_wraps_shell_metacharacters() {
        assert_eq!(quote("plain"), "plain");
        assert_eq!(quote("two words"), "'two words'");
        assert_eq!(quote("semi;colon"), "'semi;colon'");
    }

    #[test]
    fn join_quotes_each_part() {
        assert_eq!(
            join(["git", "commit", "-m", "fix: 带 空格"]),
            "git commit -m 'fix: 带 空格'"
        );
    }

    #[test]
    fn run_checked_chains_in_one_shell() {
        // export 对后一条生效才说明两条在同一个进程里
        let dir = tempfile::tempdir().unwrap();
        let commands = vec![
            "export FUNBUILD_PROBE=1".to_string(),
            "test \"$FUNBUILD_PROBE\" = 1".to_string(),
        ];
        run_checked(&commands, Some(dir.path())).unwrap();
    }

    #[test]
    fn run_checked_reports_failing_chain() {
        let error = run_checked(&["true".to_string(), "exit 3".to_string()], None).unwrap_err();
        match error {
            Error::Shell { code, command } => {
                assert_eq!(code, Some(3));
                assert_eq!(command, "true && exit 3");
            }
            other => panic!("unexpected: {other}"),
        }
    }

    #[test]
    fn capture_separates_streams() {
        let output = capture("printf out; printf err >&2; exit 2", None, None).unwrap();
        assert_eq!(output.stdout, "out");
        assert_eq!(output.stderr, "err");
        assert_eq!(output.code, 2);
    }

    #[test]
    fn capture_survives_output_larger_than_pipe_buffer() {
        // 不并发读管道的实现会在这里挂死
        let output = capture(
            "yes abcdefgh | head -c 500000",
            None,
            Some(Duration::from_secs(30)),
        )
        .unwrap();
        assert_eq!(output.stdout.len(), 500_000);
    }

    #[test]
    fn capture_times_out() {
        let error = capture("sleep 5", None, Some(Duration::from_millis(200))).unwrap_err();
        assert!(
            matches!(error, Error::Timeout { .. }),
            "unexpected: {error}"
        );
    }
}
