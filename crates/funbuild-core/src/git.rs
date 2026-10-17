use std::path::{Path, PathBuf};
use std::time::{Duration, UNIX_EPOCH};

use crate::error::{Error, Result};
use crate::shell;

pub const DEFAULT_ORG: &str = "farfarfun";

/// 仓库根目录。
pub fn repo_root(cwd: &Path) -> Result<PathBuf> {
    let output = shell::capture("git rev-parse --show-toplevel", Some(cwd), None)?;
    let root = output.trimmed();
    if !output.ok() || root.is_empty() || !Path::new(root).is_dir() {
        return Err(Error::NotAGitRepository(cwd.display().to_string()));
    }
    Ok(PathBuf::from(root))
}

/// 这个仓库是否属于本组织——问 remote，不看名字前缀。
///
/// 组织经历过 note* -> fun* -> 部分 nlt* 的改名，前缀早就不能代表归属。
pub fn is_org_repo(repo: &Path, org: &str) -> bool {
    let command = shell::join([
        "git",
        "-C",
        &repo.display().to_string(),
        "remote",
        "get-url",
        "origin",
    ]);
    let url = shell::capture_stdout(&command, None, Some(Duration::from_secs(10))).to_lowercase();
    // 同时匹配 https://github.com/<org>/x 与 git@github.com:<org>/x
    url.contains(&format!("/{org}/")) || url.contains(&format!(":{org}/"))
}

#[derive(Debug, Clone)]
pub struct Change {
    pub modified: u128,
    /// 新路径；rename/copy 时 `paths` 里还带着旧路径。
    pub path: String,
    pub paths: Vec<String>,
}

/// 工作区改动，按 mtime 从旧到新排序。
pub fn changed_files(repo: &Path) -> Result<Vec<Change>> {
    let output = shell::capture(
        "git status --porcelain=v1 -z --untracked-files=all",
        Some(repo),
        None,
    )?;
    if !output.ok() {
        return Err(Error::Shell {
            command: "git status --porcelain=v1 -z --untracked-files=all".into(),
            code: Some(output.code),
        });
    }

    let fields: Vec<&str> = output.stdout.split('\0').collect();
    let mut changes = Vec::new();
    let mut index = 0;
    while index < fields.len() && !fields[index].is_empty() {
        let record = fields[index];
        if record.len() < 3 {
            index += 1;
            continue;
        }
        let status = &record[..2];
        let mut paths = vec![record[3..].to_string()];
        if status.contains('R') || status.contains('C') {
            index += 1;
            if let Some(old) = fields.get(index) {
                paths.push((*old).to_string());
            }
        }
        changes.push(Change {
            modified: mtime_nanos(&repo.join(&paths[0])),
            path: paths[0].clone(),
            paths,
        });
        index += 1;
    }
    changes.sort_by(|a, b| (a.modified, &a.path).cmp(&(b.modified, &b.path)));
    Ok(changes)
}

fn mtime_nanos(path: &Path) -> u128 {
    std::fs::symlink_metadata(path)
        .and_then(|meta| meta.modified())
        .map(|time| {
            time.duration_since(UNIX_EPOCH)
                .unwrap_or(Duration::ZERO)
                .as_nanos()
        })
        .unwrap_or(0)
}

/// 暂存区是否有待提交内容。
pub fn has_staged_changes(cwd: Option<&Path>) -> Result<bool> {
    let code = shell::status("git diff --staged --quiet", cwd)?;
    match code {
        0 => Ok(false),
        1 => Ok(true),
        other => Err(Error::Shell {
            command: "git diff --staged --quiet".into(),
            code: Some(other),
        }),
    }
}

/// 已初始化的直接 submodule 绝对路径。未初始化的（`git submodule status` 里以
/// "-" 打头）没法 push，过滤掉。
pub fn submodule_paths(repo: &Path) -> Vec<PathBuf> {
    let output = shell::capture_stdout("git submodule status", Some(repo), None);
    output
        .lines()
        .filter(|line| !line.is_empty() && !line.starts_with('-'))
        .filter_map(|line| line.split_whitespace().nth(1))
        .map(|rel| repo.join(rel))
        .collect()
}

/// 当前分支名，取不到时回退 master。
pub fn current_branch(cwd: Option<&Path>) -> String {
    let branch = shell::capture_stdout("git rev-parse --abbrev-ref HEAD", cwd, None)
        .trim()
        .to_string();
    if branch.is_empty() {
        "master".to_string()
    } else {
        branch
    }
}

pub fn last_commit_message(cwd: Option<&Path>) -> String {
    shell::capture_stdout("git log -1 --format=%B", cwd, None)
}

#[cfg(test)]
pub(crate) mod testing {
    use std::path::Path;

    /// 建一个能跑 git 命令的空仓库。
    pub fn init_repo(path: &Path) {
        let commands = [
            "git init --quiet",
            "git config user.email test@example.com",
            "git config user.name test",
            "git config commit.gpgsign false",
        ]
        .map(String::from);
        crate::shell::run_checked(&commands, Some(path)).unwrap();
    }

    pub fn commit_all(path: &Path, message: &str) {
        let commands = vec![
            "git add -A".to_string(),
            crate::shell::join(["git", "commit", "--quiet", "-m", message]),
        ];
        crate::shell::run_checked(&commands, Some(path)).unwrap();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_root_resolves_from_subdirectory() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        let nested = dir.path().join("a/b");
        std::fs::create_dir_all(&nested).unwrap();

        let root = repo_root(&nested).unwrap();
        // macOS 的 /var -> /private/var 之类，两边都 canonicalize 再比
        assert_eq!(
            root.canonicalize().unwrap(),
            dir.path().canonicalize().unwrap()
        );
    }

    #[test]
    fn repo_root_rejects_non_repository() {
        let dir = tempfile::tempdir().unwrap();
        let error = repo_root(dir.path()).unwrap_err();
        assert!(matches!(error, Error::NotAGitRepository(_)), "{error}");
    }

    #[test]
    fn changed_files_sorted_by_mtime() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        std::fs::write(dir.path().join("old.txt"), "a").unwrap();
        // 隔开 mtime，否则同一纳秒内无法断言顺序
        std::thread::sleep(Duration::from_millis(20));
        std::fs::write(dir.path().join("new.txt"), "b").unwrap();

        let changes = changed_files(dir.path()).unwrap();
        let paths: Vec<&str> = changes.iter().map(|c| c.path.as_str()).collect();
        assert_eq!(paths, vec!["old.txt", "new.txt"]);
    }

    #[test]
    fn changed_files_keeps_both_sides_of_a_rename() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        std::fs::write(dir.path().join("before.txt"), "content").unwrap();
        testing::commit_all(dir.path(), "chore: 初始");
        std::fs::rename(dir.path().join("before.txt"), dir.path().join("after.txt")).unwrap();
        crate::shell::run_checked(&["git add -A".to_string()], Some(dir.path())).unwrap();

        let changes = changed_files(dir.path()).unwrap();
        let renamed = changes
            .iter()
            .find(|c| c.paths.len() == 2)
            .expect("rename record");
        assert_eq!(renamed.path, "after.txt");
        assert!(renamed.paths.contains(&"before.txt".to_string()));
    }

    #[test]
    fn staged_changes_detected() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        assert!(!has_staged_changes(Some(dir.path())).unwrap());
        std::fs::write(dir.path().join("a.txt"), "x").unwrap();
        crate::shell::run_checked(&["git add -A".to_string()], Some(dir.path())).unwrap();
        assert!(has_staged_changes(Some(dir.path())).unwrap());
    }

    #[test]
    fn org_repo_matches_both_remote_forms() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        crate::shell::run_checked(
            &["git remote add origin git@github.com:farfarfun/demo.git".to_string()],
            Some(dir.path()),
        )
        .unwrap();
        assert!(is_org_repo(dir.path(), DEFAULT_ORG));

        let other = tempfile::tempdir().unwrap();
        testing::init_repo(other.path());
        crate::shell::run_checked(
            &["git remote add origin https://github.com/someone-else/demo.git".to_string()],
            Some(other.path()),
        )
        .unwrap();
        assert!(!is_org_repo(other.path(), DEFAULT_ORG));
    }

    #[test]
    fn no_submodules_is_empty() {
        let dir = tempfile::tempdir().unwrap();
        testing::init_repo(dir.path());
        assert!(submodule_paths(dir.path()).is_empty());
    }
}
