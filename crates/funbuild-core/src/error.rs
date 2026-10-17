use std::path::{Path, PathBuf};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("shell command chain failed (exit={code:?}): {command}")]
    Shell { command: String, code: Option<i32> },

    #[error("命令 {timeout_secs}s 未返回: {command}")]
    Timeout { command: String, timeout_secs: u64 },

    #[error("当前目录不在 git 仓库中: {0}")]
    NotAGitRepository(String),

    #[error("无法解析 {kind} 文件 {path}: {message}")]
    ManifestParse {
        kind: &'static str,
        path: PathBuf,
        message: String,
    },

    #[error("{0}")]
    BuilderDetection(String),

    #[error("{0}")]
    ManifestVersionSync(String),

    #[error("{0}")]
    LatestDependency(String),

    #[error("无法读取 {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },

    #[error("无法写入 {path}: {source}")]
    Write {
        path: PathBuf,
        source: std::io::Error,
    },

    #[error("{0}")]
    Io(#[from] std::io::Error),

    #[error("{0}")]
    Invalid(String),
}

impl Error {
    pub(crate) fn manifest_parse(
        kind: &'static str,
        path: impl Into<PathBuf>,
        message: impl std::fmt::Display,
    ) -> Self {
        Error::ManifestParse {
            kind,
            path: path.into(),
            message: message.to_string(),
        }
    }

    pub(crate) fn version_sync(message: impl Into<String>) -> Self {
        Error::ManifestVersionSync(message.into())
    }

    pub(crate) fn latest_dependency(message: impl Into<String>) -> Self {
        Error::LatestDependency(message.into())
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Error::Invalid(message.into())
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// 带路径的读写，取代裸 `std::fs` —— 多清单仓库下 io::Error 本身看不出是哪个文件。
pub(crate) fn read_to_string(path: impl AsRef<Path>) -> Result<String> {
    let path = path.as_ref();
    std::fs::read_to_string(path).map_err(|source| Error::Read {
        path: path.to_path_buf(),
        source,
    })
}

pub(crate) fn write(path: impl AsRef<Path>, contents: impl AsRef<[u8]>) -> Result<()> {
    let path = path.as_ref();
    std::fs::write(path, contents).map_err(|source| Error::Write {
        path: path.to_path_buf(),
        source,
    })
}
