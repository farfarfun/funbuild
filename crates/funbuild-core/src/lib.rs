//! funbuild 的发版流程实现。
//!
//! CLI（`crates/funbuild`）只做参数解析，所有逻辑在这里。

pub mod builders;
pub mod commit;
pub mod error;
pub mod git;
pub mod latest_deps;
pub mod manifest;
pub mod shell;
pub mod util;
pub mod version;

pub use error::{Error, Result};
