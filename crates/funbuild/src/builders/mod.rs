//! 构建类型与公共发版流程。
//!
//! Python 版靠类继承，这里统一用组合：MaturinBuild 持有一个 UvBuild，状态只存在
//! 内层那一份，`ctx()` 转发过去，不会出现两份会漂移的 name/version。

use std::cell::OnceCell;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::error::{Error, Result};
use crate::{commit, git, shell, version};

mod cargo;
mod empty;
mod flutter;
mod hybrid;
mod maturin;
pub(crate) mod npm;
mod poetry;
mod pypi;
mod submodule_workspace;
pub(crate) mod uv;
mod version_file;

pub use cargo::CargoBuild;
pub use empty::EmptyBuild;
pub use flutter::FlutterBuild;
pub use hybrid::UvNpmHybridBuild;
pub use maturin::MaturinBuild;
pub use npm::NpmFrontendBuild;
pub use poetry::PoetryBuild;
pub use pypi::PypiBuild;
pub use submodule_workspace::SubmoduleWorkspaceBuild;
pub use uv::UvBuild;
pub use version_file::VersionFileBuild;

/// 所有 builder 共享的仓库状态。
pub struct RepoContext {
    pub repo_path: PathBuf,
    pub name: String,
    pub version: Option<String>,
    org: OnceCell<bool>,
}

impl RepoContext {
    pub fn new(repo_path: PathBuf) -> Self {
        let name = repo_path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        Self {
            repo_path,
            name,
            version: None,
            org: OnceCell::new(),
        }
    }

    /// 仓库归属本组织时才自动套用组织约定（许可证 / authors / urls / ruff）。
    ///
    /// remote 缺失或临时不可读时退回前缀判断，保证离线也能工作。
    pub fn is_org_repo(&self) -> bool {
        *self.org.get_or_init(|| {
            git::is_org_repo(&self.repo_path, git::DEFAULT_ORG)
                || ["fun", "nlt", "note"]
                    .iter()
                    .any(|p| self.name.starts_with(p))
        })
    }

    pub fn cwd(&self) -> Option<&Path> {
        Some(&self.repo_path)
    }
}

/// 清理构建产物。
///
/// 这里只能列「生成物」。`build()` 在 publish 之后还会再跑一次本方法，紧接着就是
/// push——任何受版本控制的文件列进来，都会被当成「用户删除了它」提交上去。
pub fn default_delete_commands() -> Vec<String> {
    [
        "rm -rf dist",
        "rm -rf extbuild/*/dist",
        "rm -rf build",
        "rm -rf extbuild/*/build",
        "rm -rf *.egg-info",
        "rm -rf extbuild/*/src/*.egg-info",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

pub trait Builder {
    fn ctx(&self) -> &RepoContext;
    fn ctx_mut(&mut self) -> &mut RepoContext;

    /// 构建类型名，只用于日志。
    fn kind(&self) -> &'static str;

    /// 把当前版本号写回本构建类型的版本清单。
    fn write_version(&mut self) -> Result<()>;

    fn cmd_build(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    fn cmd_publish(&self) -> Result<Vec<String>> {
        Ok(Vec::new())
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(vec!["pip install dist/*.whl --force-reinstall".to_string()])
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        Ok(default_delete_commands())
    }

    /// 发版前把「每次取最新」的依赖下界抬到最新已发布版本。默认什么都不做。
    fn sync_latest_dependencies(&mut self) -> Result<()> {
        Ok(())
    }

    /// 本次发布的分发名 -> 可直接拿去安装的项目目录。
    fn published_distributions(&self) -> Result<BTreeMap<String, PathBuf>> {
        let ctx = self.ctx();
        Ok([(ctx.name.clone(), ctx.repo_path.clone())].into())
    }

    fn upgrade(&mut self, target: Option<&str>) -> Result<()> {
        let next = match target {
            Some(value) => version::strip_v_prefix(value).to_string(),
            None => version::next_version(self.ctx().version.as_deref())?,
        };
        self.ctx_mut().version = Some(next);
        self.write_version()
    }

    fn pull(&self) -> Result<()> {
        log::info!("{} pull", self.ctx().name);
        shell::run_checked(&["git pull".to_string()], self.ctx().cwd())
    }

    /// 把改动按 mtime 从旧到新分批提交并推送。
    ///
    /// `message` 为 None 时交给 aicommits 生成；显式传入则原样使用——aicommits 会
    /// 无视外部信息自己生成一条，所以指定了信息就不能再走它。
    fn push(&mut self, message: Option<&str>, batch_size: usize) -> Result<()> {
        log::info!("{} push", self.ctx().name);
        if batch_size < 1 {
            return Err(Error::invalid("batch_size must be at least 1"));
        }
        commit::warn_if_invalid(message);

        let repo = self.ctx().repo_path.clone();
        let cwd = Some(repo.as_path());
        let changes = git::changed_files(&repo)?;
        if !changes.is_empty() {
            shell::run_checked(&["git reset --quiet".to_string()], cwd)?;
        }
        for batch in changes.chunks(batch_size) {
            let mut paths: Vec<&str> = Vec::new();
            for change in batch {
                for path in &change.paths {
                    if !paths.contains(&path.as_str()) {
                        paths.push(path);
                    }
                }
            }
            let mut add = vec![
                "git".to_string(),
                "add".into(),
                "-A".into(),
                "-f".into(),
                "--".into(),
            ];
            add.extend(paths.iter().map(|p| p.to_string()));
            shell::run_checked(&[shell::join(&add)], cwd)?;

            // 本批内容可能已被上一次提交带走（如 aicommits 提交了全部暂存内容）
            if !git::has_staged_changes(cwd)? {
                continue;
            }
            if message.is_none() && commit::aicommits_commit(cwd, commit::DEFAULT_MESSAGE)? {
                continue;
            }
            let text = message.unwrap_or(commit::DEFAULT_MESSAGE);
            shell::run_checked(&[shell::join(["git", "commit", "-m", text])], cwd)?;
        }
        shell::run_checked(&["git push".to_string()], cwd)
    }

    /// 先在每个 submodule 里执行一次 `funbuild push`，再 push 当前仓库。
    ///
    /// 走子进程而非复用 self.push()，使其行为与手动 cd 进去执行完全一致——包括各
    /// submodule 自身的构建类型探测。不递归到 submodule 的 submodule。
    fn push_all(&mut self, message: Option<&str>, batch_size: usize) -> Result<()> {
        // 先校验再下潜：否则非法信息会在每个 submodule 的子进程里各告警一次
        commit::warn_if_invalid(message);
        for path in git::submodule_paths(&self.ctx().repo_path) {
            log::info!("push submodule: {}", path.display());
            let mut cmd = vec![self_command(), "push".to_string()];
            if let Some(message) = message {
                cmd.push("--message".into());
                cmd.push(message.to_string());
            }
            cmd.push("--batch-size".into());
            cmd.push(batch_size.to_string());
            shell::run_checked(&[shell::join(&cmd)], Some(&path))?;
        }
        self.push(message, batch_size)
    }

    /// 本地构建并安装，用于验证当前代码可安装，不发布也不打标签。
    fn install(&mut self) -> Result<()> {
        log::info!("{} install", self.ctx().name);
        let mut commands = self.cmd_build()?;
        commands.extend(self.cmd_install()?);
        commands.extend(self.cmd_delete()?);
        shell::run_checked(&commands, self.ctx().cwd())
    }

    /// 完整发布流程。任一步失败立即中止，不会继续 push 或打标签。
    fn build(
        &mut self,
        message: Option<&str>,
        target_version: Option<&str>,
        dry_run: bool,
        apps: &[String],
    ) -> Result<()> {
        if dry_run || !apps.is_empty() {
            return Err(Error::invalid(
                "--dry-run and --apps are only supported for submodule workspaces",
            ));
        }
        // 必须在 publish 之前校验：push 是发布之后才跑的，等到那时才报错，包已经
        // 上了 PyPI，却留下「线上有这个版本、仓库里没有对应提交和 tag」的半截状态。
        commit::warn_if_invalid(message);
        log::info!("{} build", self.ctx().name);
        self.pull()?;
        self.upgrade(target_version)?;
        // 必须在构建之前：改的是依赖下界，要进这一次的 wheel metadata 才有意义
        self.sync_latest_dependencies()?;

        let mut commands = self.cmd_delete()?;
        commands.extend(self.cmd_build()?);
        commands.extend(self.cmd_install()?);
        commands.extend(self.cmd_publish()?);
        commands.extend(self.cmd_delete()?);
        shell::run_checked(&commands, self.ctx().cwd())?;

        self.push(message, 20)?;
        self.tags()?;
        // 放在最后：这一步在替换 funbuild 自己，万一出问题也已经 push 过、tag 过
        self.install_into_running_env();
        Ok(())
    }

    /// 为当前版本号打 `v<version>` 标签并强制推送。
    fn tags(&self) -> Result<()> {
        let Some(version) = self.ctx().version.clone() else {
            log::warn!("skip tags: version is not set");
            return Ok(());
        };
        let tag = shell::quote(&format!("v{version}"));
        shell::run_checked(
            &[
                format!("git tag --force {tag}"),
                // 只推这一个 tag 且强制覆盖。`git push --tags` 会把本地所有 tag 一并
                // 推上去，且拒绝更新远端已存在的 tag——与上一行的意图直接矛盾。
                format!("git push --force origin {tag}"),
            ],
            self.ctx().cwd(),
        )
    }

    /// 清理 git 索引缓存并重新提交，使新增的 .gitignore 规则对已跟踪文件生效。
    fn clean(&self) -> Result<()> {
        log::info!("{} clean", self.ctx().name);
        let cwd = self.ctx().cwd();
        shell::run_checked(
            &["git rm -r --cached .".to_string(), "git add .".to_string()],
            cwd,
        )?;
        if git::has_staged_changes(cwd)? {
            shell::run_checked(&["git commit -m 'chore: 更新忽略规则'".to_string()], cwd)?;
        }
        shell::run_checked(&["git gc --aggressive".to_string()], cwd)
    }

    /// 抹掉全部 git 历史与标签并强推当前分支，不可恢复且无二次确认。
    fn clean_history(&self) -> Result<()> {
        log::info!("{} clean history", self.ctx().name);
        let cwd = self.ctx().cwd();
        let branch = git::current_branch(cwd);
        let name = &self.ctx().name;
        shell::run_checked(
            &[
                "git tag -d $(git tag -l) || true".to_string(),
                "git fetch".to_string(),
                // 无 tag 时 `git push origin --delete` 会因缺少参数报错，故先判空
                "[ -z \"$(git tag -l)\" ] || git push origin --delete $(git tag -l)".to_string(),
                "git tag -d $(git tag -l) || true".to_string(),
                "git checkout --orphan latest_branch".to_string(),
                "git add -A".to_string(),
                "git commit -am \"chore: 清理提交历史\"".to_string(),
                format!("git branch -D {branch} || true"),
                format!("git branch -m {branch}"),
                format!("git push -f origin {branch}"),
                format!("git push --set-upstream origin {branch}"),
                format!("echo {name} success"),
            ],
            cwd,
        )
    }

    /// 把刚发布的版本装进「正在运行 funbuild 的那个环境」。
    ///
    /// `cmd_install` 那一步装的是项目自己的 `.venv`，而用户敲的命令往往装在别处。
    /// 只升级这个环境里**已经装过**的分发，没装过的不碰。装的是工作区而不走 PyPI
    /// ——索引有几分钟传播延迟，刚发的版本常常拉到上一版。
    ///
    /// 失败只告警：版本已发布、提交和 tag 都在，重跑一条命令即可。
    fn install_into_running_env(&self) {
        let Some(env) = RunningEnv::detect() else {
            return;
        };
        // 跑在项目自己的 .venv 里时，安装校验那一步装的就是这个环境
        let own_venv = self.ctx().repo_path.join(".venv");
        if env.is_same_prefix(&own_venv) {
            return;
        }
        let distributions = match self.published_distributions() {
            Ok(distributions) => distributions,
            Err(error) => {
                log::warn!("跳过本机升级: 无法确定本次发布的分发名 ({error})");
                return;
            }
        };
        for (dist, project_dir) in distributions {
            let Some(installed) = env.installed_version(&dist) else {
                continue;
            };
            if !project_dir.is_dir() {
                continue;
            }
            let command = shell::join([
                "uv",
                "pip",
                "install",
                "--python",
                &env.python.display().to_string(),
                &project_dir.display().to_string(),
            ]);
            log::info!(
                "升级 {} 里的 {dist} (当前 {installed})",
                env.prefix.display()
            );
            if let Err(error) = shell::run_checked(std::slice::from_ref(&command), None) {
                log::warn!("{dist} 的本机升级失败, 版本已发布, 手动重试: {command} ({error})");
            }
        }
    }
}

/// 起子进程调自己时用的命令。
///
/// 优先用 `current_exe()` 而不是裸 `funbuild`：PATH 上完全可能是另一个版本。
fn self_command() -> String {
    std::env::current_exe()
        .ok()
        .filter(|path| path.is_file())
        .map(|path| path.display().to_string())
        .unwrap_or_else(|| "funbuild".to_string())
}

/// 正在运行 funbuild 的那个 Python 环境。
///
/// 二进制装在 wheel 的 `<venv>/bin/funbuild`，所以 venv 根就是它的祖父目录。
struct RunningEnv {
    prefix: PathBuf,
    python: PathBuf,
}

impl RunningEnv {
    fn detect() -> Option<Self> {
        let exe = std::env::current_exe().ok()?;
        let prefix = exe.parent()?.parent()?.to_path_buf();
        let python = prefix.join("bin/python");
        python.is_file().then_some(Self { prefix, python })
    }

    fn is_same_prefix(&self, candidate: &Path) -> bool {
        match (candidate.canonicalize(), self.prefix.canonicalize()) {
            (Ok(a), Ok(b)) => a == b,
            _ => false,
        }
    }

    /// 这个环境里装着的版本，没装返回 None。
    fn installed_version(&self, dist: &str) -> Option<String> {
        let command = shell::join([
            "uv",
            "pip",
            "show",
            dist,
            "--python",
            &self.python.display().to_string(),
        ]);
        let output = shell::capture(&command, None, None).ok()?;
        if !output.ok() {
            return None;
        }
        output
            .stdout
            .lines()
            .find_map(|line| line.strip_prefix("Version:"))
            .map(|value| value.trim().to_string())
    }
}

/// 探测构建类型。顺序即优先级，越靠前越具体。
///
/// 单个 builder 探测出错不中断整条链（否则会因某个不相关的清单异常而让本可正确
/// 匹配的后续 builder 没有机会被尝试），但异常要留痕：全部落空时据此决定是兜底
/// 还是报错，不能一声不吭地退化成空构建。
pub fn detect(repo_path: PathBuf) -> Result<Box<dyn Builder>> {
    let mut failures: Vec<String> = Vec::new();

    macro_rules! try_builder {
        ($ty:ty) => {
            match <$ty>::detect(&repo_path) {
                Ok(Some(builder)) => return Ok(Box::new(builder)),
                Ok(None) => {}
                Err(error) => {
                    let kind = stringify!($ty);
                    log::warn!("{kind} check_type failed, skipped: {error}");
                    failures.push(format!("{kind}: {error}"));
                }
            }
        };
    }

    // 顺序照 registry.py；还没搬完的构建类型会依次插进来
    // 工作区配置决定发布范围；损坏时不能回退到其他 builder 发布根仓库。
    if let Some(builder) = SubmoduleWorkspaceBuild::detect(&repo_path)? {
        return Ok(Box::new(builder));
    }
    try_builder!(UvNpmHybridBuild);
    try_builder!(MaturinBuild);
    try_builder!(UvBuild);
    try_builder!(PoetryBuild);
    try_builder!(PypiBuild);
    try_builder!(FlutterBuild);
    try_builder!(NpmFrontendBuild);
    try_builder!(CargoBuild);
    try_builder!(VersionFileBuild);

    if !failures.is_empty() {
        return Err(Error::BuilderDetection(format!(
            "未识别到可用的构建类型, 且探测过程中有清单文件解析失败; 拒绝退化为空构建。\n  - {}",
            failures.join("\n  - ")
        )));
    }

    // 调一次保留 EmptyBuild 自己那条「回退到空构建」的告警
    let fallback = EmptyBuild::fallback(repo_path);
    Ok(Box::new(fallback))
}

#[cfg(test)]
mod tests {
    use super::*;

    const PYPROJECT: &str = "[project]\nname = \"demo\"\nversion = \"1.0.0\"\n";
    const PACKAGE_JSON: &str =
        r#"{"name":"demo","version":"1.0.0","scripts":{"build":"vite build"}}"#;
    const FLUTTER_PUBSPEC: &str =
        "name: demo\nversion: 1.0.0+1\ndependencies:\n  flutter:\n    sdk: flutter\n";

    fn write(dir: &Path, rel: &str, body: &str) {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    fn kind_of(dir: &Path) -> &'static str {
        detect(dir.to_path_buf()).unwrap().kind()
    }

    #[test]
    fn workspace_wins_over_everything() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "scripts/funbuild.toml", "version = \"1.0.0\"\n");
        std::fs::create_dir_all(dir.path().join("apps")).unwrap();
        // 即使同时是个 uv 包，编排仓库也该优先接管
        write(dir.path(), "pyproject.toml", PYPROJECT);
        assert_eq!(kind_of(dir.path()), "SubmoduleWorkspaceBuild");
    }

    #[test]
    fn python_plus_frontend_is_hybrid() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "pyproject.toml", PYPROJECT);
        write(dir.path(), "package.json", PACKAGE_JSON);
        assert_eq!(kind_of(dir.path()), "UvNpmHybridBuild");
    }

    #[test]
    fn maturin_claimed_before_plain_uv() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "pyproject.toml",
            "[build-system]\nbuild-backend = \"maturin\"\n\n[project]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        write(dir.path(), "Cargo.toml", "[package]\nversion = \"1.0.0\"\n");
        assert_eq!(kind_of(dir.path()), "MaturinBuild");
    }

    #[test]
    fn plain_pep621_is_uv() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "pyproject.toml", PYPROJECT);
        assert_eq!(kind_of(dir.path()), "UVBuild");
    }

    #[test]
    fn poetry_only_when_no_project_table() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "pyproject.toml",
            "[tool.poetry]\nname = \"demo\"\nversion = \"1.0.0\"\n",
        );
        assert_eq!(kind_of(dir.path()), "PoetryBuild");
    }

    #[test]
    fn flutter_claimed_before_frontend() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "pubspec.yaml", FLUTTER_PUBSPEC);
        // Flutter Web 项目常带一个仅供前端工具链用的 package.json
        write(dir.path(), "package.json", PACKAGE_JSON);
        assert_eq!(kind_of(dir.path()), "FlutterBuild");
    }

    #[test]
    fn frontend_only_repo() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "package.json", PACKAGE_JSON);
        assert_eq!(kind_of(dir.path()), "NpmFrontendBuild");
    }

    #[test]
    fn pure_rust_repo() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "Cargo.toml",
            "[package]\nname = \"demo\"\nversion = \"0.3.0\"\n",
        );
        assert_eq!(kind_of(dir.path()), "CargoBuild");
    }

    #[test]
    fn cargo_claimed_before_version_file() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "Cargo.toml", "[package]\nversion = \"0.3.0\"\n");
        // 并排的 VERSION 只会是过期副本
        write(dir.path(), "VERSION", "0.0.1\n");
        assert_eq!(kind_of(dir.path()), "CargoBuild");
    }

    #[test]
    fn version_file_is_the_last_real_fallback() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "VERSION", "1.2.3\n");
        assert_eq!(kind_of(dir.path()), "VersionFileBuild");
    }

    #[test]
    fn pypi_script_repo() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "script/__version__.md", "1.2.3");
        assert_eq!(kind_of(dir.path()), "PypiBuild");
    }

    #[test]
    fn empty_repo_falls_back() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(kind_of(dir.path()), "EmptyBuild");
    }

    #[test]
    fn broken_manifest_refuses_to_degrade_into_empty() {
        let dir = tempfile::tempdir().unwrap();
        // pyproject 写坏了：退化成 EmptyBuild 会让 build 什么都不做却以退出码 0 结束
        write(dir.path(), "pyproject.toml", "[project]\nname = \"demo\n");
        let error = match detect(dir.path().to_path_buf()) {
            Err(error) => error,
            Ok(builder) => panic!("本该报错, 却认领成了 {}", builder.kind()),
        };
        assert!(matches!(error, Error::BuilderDetection(_)), "{error}");
        assert!(error.to_string().contains("拒绝退化为空构建"), "{error}");
    }

    #[test]
    fn one_broken_manifest_does_not_block_a_later_match() {
        let dir = tempfile::tempdir().unwrap();
        // package.json 坏了，但 VERSION 文件仍该被认领
        write(dir.path(), "package.json", "{broken");
        write(dir.path(), "VERSION", "1.2.3\n");
        assert_eq!(kind_of(dir.path()), "VersionFileBuild");
    }
}
