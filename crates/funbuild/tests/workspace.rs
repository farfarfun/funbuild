use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn command(program: &str, cwd: &Path) -> Command {
    let mut cmd = Command::new(program);
    cmd.current_dir(cwd)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1");
    cmd
}

fn output_text(output: &Output) -> String {
    format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn git(cwd: &Path, args: &[&str]) -> String {
    let output = command("git", cwd).args(args).output().unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        output_text(&output)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}

fn identity(path: &Path) {
    git(path, &["config", "user.name", "test"]);
    git(path, &["config", "user.email", "test@example.com"]);
}

fn commit(path: &Path) {
    git(path, &["add", "-A"]);
    git(path, &["commit", "--quiet", "-m", "chore: fixture"]);
}

// 每个 app 和工作区都只推送到临时目录中的 bare 仓库，不访问发布服务。
struct Workspace {
    temp: tempfile::TempDir,
    root: PathBuf,
}

impl Workspace {
    fn new(paths: &[&str], config: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("workspace");
        std::fs::create_dir(&root).unwrap();
        git(&root, &["init", "--quiet", "--initial-branch=main"]);
        identity(&root);
        std::fs::create_dir(root.join("apps")).unwrap();
        std::fs::create_dir(root.join("scripts")).unwrap();
        std::fs::write(root.join("scripts/funbuild.toml"), config).unwrap();
        for (index, path) in paths.iter().enumerate() {
            let seed = temp.path().join(format!("seed-{index}"));
            let remote = temp.path().join(format!("remote-{index}.git"));
            std::fs::create_dir(&seed).unwrap();
            git(&seed, &["init", "--quiet", "--initial-branch=main"]);
            identity(&seed);
            std::fs::write(seed.join("VERSION"), "1.0.0\n").unwrap();
            commit(&seed);
            git(
                temp.path(),
                &[
                    "clone",
                    "--quiet",
                    "--bare",
                    seed.to_str().unwrap(),
                    remote.to_str().unwrap(),
                ],
            );
            git(
                &root,
                &[
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    "--quiet",
                    remote.to_str().unwrap(),
                    path,
                ],
            );
            identity(&root.join(path));
        }
        commit(&root);
        let remote = temp.path().join("workspace.git");
        git(
            temp.path(),
            &[
                "init",
                "--quiet",
                "--bare",
                "--initial-branch=main",
                remote.to_str().unwrap(),
            ],
        );
        git(
            &root,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(
            &root,
            &["push", "--quiet", "--set-upstream", "origin", "main"],
        );
        Self { temp, root }
    }

    fn run(&self, args: &[&str]) -> Output {
        command(env!("CARGO_BIN_EXE_funbuild"), &self.root)
            .args(args)
            .output()
            .unwrap()
    }

    fn version(&self, app: &str) -> String {
        std::fs::read_to_string(self.root.join(app).join("VERSION")).unwrap()
    }

    fn config(&self) -> String {
        std::fs::read_to_string(self.root.join("scripts/funbuild.toml")).unwrap()
    }
}

#[test]
fn preview_lists_order_and_skip_reasons_without_changes() {
    let workspace = Workspace::new(
        &[
            "apps/a",
            "apps/b",
            "apps/c",
            "apps/with space",
            "apps/offline",
            "apps/nested",
            "libs/b",
        ],
        "version = \"1.0.0\"\nexclude-apps = [\"b\"]\nbuild-order = [\"c\", \"b\"]\n",
    );
    git(
        &workspace.root,
        &["submodule", "deinit", "--force", "apps/offline"],
    );
    let nested = workspace.root.join("apps/nested");
    std::fs::create_dir(nested.join("apps")).unwrap();
    std::fs::create_dir(nested.join("scripts")).unwrap();
    std::fs::write(nested.join("scripts/setup.sh"), "#!/bin/sh\n").unwrap();
    git(
        &workspace.root,
        &[
            "remote",
            "set-url",
            "origin",
            "/nonexistent-funbuild-test-remote",
        ],
    );
    let before = git(&workspace.root, &["status", "--porcelain=v1"]);
    let head = git(&workspace.root, &["rev-parse", "HEAD"]);
    let config = workspace.config();
    let output = workspace.run(&["build", "--dry-run"]);
    let text = output_text(&output);
    assert!(output.status.success(), "{text}");
    assert!(text.contains("目标版本 1.0.1"), "{text}");
    assert!(
        text.contains("1. apps/c\n  2. apps/a\n  3. apps/with space"),
        "{text}"
    );
    for reason in [
        "跳过 apps/b: exclude-apps",
        "跳过 libs/b: 不在 apps/",
        "跳过 apps/offline: 子模块未初始化",
        "跳过 apps/nested: 嵌套工作区",
    ] {
        assert!(text.contains(reason), "{text}");
    }
    assert_eq!(workspace.config(), config);
    assert_eq!(git(&workspace.root, &["status", "--porcelain=v1"]), before);
    assert_eq!(git(&workspace.root, &["rev-parse", "HEAD"]), head);
    assert!(git(&workspace.root, &["tag"]).is_empty());
    assert_eq!(workspace.version("apps/a"), "1.0.0\n");
}

#[test]
fn selected_apps_still_obey_exclusions_and_scope() {
    let workspace = Workspace::new(
        &["apps/a", "apps/b", "apps/c", "libs/b"],
        "version = \"1.0.0\"\nexclude-apps = [\"b\"]\n",
    );
    let output = workspace.run(&["build", "chore: test", "--apps", "a,b,a"]);
    let text = output_text(&output);
    assert!(output.status.success(), "{text}");
    assert_eq!(workspace.version("apps/a"), "1.0.1\n");
    for path in ["apps/b", "apps/c", "libs/b"] {
        assert_eq!(workspace.version(path), "1.0.0\n");
        assert!(git(&workspace.root.join(path), &["tag"]).is_empty());
    }
    assert!(text.contains("apps/a: 成功"), "{text}");
    assert!(text.contains("apps/b: 跳过（exclude-apps）"), "{text}");
    assert!(text.contains("apps/c: 跳过（未被 --apps 选中）"), "{text}");
    assert_eq!(git(&workspace.root, &["tag"]), "v1.0.1");
    assert!(git(&workspace.root, &["status", "--porcelain=v1"]).is_empty());
}

#[test]
fn empty_plan_does_not_pull_bump_commit_or_tag() {
    let workspace = Workspace::new(&["apps/a"], "version = \"1.0.0\"\nexclude-apps = [\"a\"]\n");
    git(
        &workspace.root,
        &["submodule", "deinit", "--force", "apps/a"],
    );
    git(
        &workspace.root,
        &[
            "remote",
            "set-url",
            "origin",
            "/nonexistent-funbuild-test-remote",
        ],
    );
    let head = git(&workspace.root, &["rev-parse", "HEAD"]);
    let config = workspace.config();
    let output = workspace.run(&["build", "chore: test"]);
    assert!(output.status.success(), "{}", output_text(&output));
    assert!(output_text(&output).contains("没有待发布应用"));
    assert_eq!(workspace.config(), config);
    assert_eq!(git(&workspace.root, &["rev-parse", "HEAD"]), head);
    assert!(git(&workspace.root, &["tag"]).is_empty());
}

#[test]
fn invalid_names_and_manifests_stop_before_mutation() {
    let workspace = Workspace::new(&["apps/a"], "version = \"1.0.0\"\n");
    let config_path = workspace.root.join("scripts/funbuild.toml");
    for config in [
        "version = \"1.0.0\"\nexclude-apps = [\"typo\"]\n",
        "version = \"1.0.0\"\nbuild-order = [\"typo\"]\n",
        "version = \"1.0.0\"\nexclude-apps = \"a\"\n",
        "version = [\n",
        "version = 123\n",
    ] {
        std::fs::write(&config_path, config).unwrap();
        // 损坏的工作区配置不能回退为根目录的 VersionFileBuild。
        std::fs::write(workspace.root.join("VERSION"), "9.0.0\n").unwrap();
        let output = workspace.run(&["build", "chore: test"]);
        assert!(
            !output.status.success(),
            "{config}: {}",
            output_text(&output)
        );
        assert_eq!(workspace.config(), config);
        assert_eq!(workspace.version("apps/a"), "1.0.0\n");
        assert_eq!(
            std::fs::read_to_string(workspace.root.join("VERSION")).unwrap(),
            "9.0.0\n"
        );
    }
    std::fs::write(&config_path, "version = \"1.0.0\"\n").unwrap();
    for args in [
        vec!["build", "--apps", "typo"],
        vec!["build", "--apps", ""],
        vec!["build", "--apps", "apps/a"],
        vec!["build", "--version", "invalid"],
    ] {
        let output = workspace.run(&args);
        assert!(
            !output.status.success(),
            "{args:?}: {}",
            output_text(&output)
        );
        assert_eq!(workspace.version("apps/a"), "1.0.0\n");
    }
    std::fs::write(workspace.root.join(".gitmodules"), "[broken\n").unwrap();
    let output = workspace.run(&["build", "--dry-run"]);
    assert!(!output.status.success());
    assert!(output_text(&output).contains(".gitmodules"));
}

#[test]
fn failure_reports_completed_failed_and_pending_apps_in_build_order() {
    let workspace = Workspace::new(
        &["apps/a", "apps/b", "apps/c"],
        "version = \"1.0.0\"\nbuild-order = [\"b\", \"a\"]\n",
    );
    let failing = workspace.root.join("apps/a");
    git(
        &failing,
        &[
            "remote",
            "set-url",
            "--push",
            "origin",
            "/nonexistent-funbuild-test-remote",
        ],
    );
    let output = workspace.run(&["build", "chore: test"]);
    let text = output_text(&output);
    assert!(!output.status.success(), "{text}");
    assert!(text.contains("apps/b: 成功"), "{text}");
    assert!(text.contains("apps/a: 失败（可能已部分发布"), "{text}");
    assert!(text.contains("apps/c: 未执行"), "{text}");
    assert_eq!(git(&workspace.root.join("apps/b"), &["tag"]), "v1.0.1");
    assert_eq!(workspace.version("apps/c"), "1.0.0\n");
    assert!(git(&workspace.root, &["tag"]).is_empty());

    git(&failing, &["config", "--unset", "remote.origin.pushurl"]);
    let output = workspace.run(&[
        "build",
        "chore: retry",
        "--apps",
        "a,c",
        "--version",
        "1.0.1",
    ]);
    assert!(output.status.success(), "{}", output_text(&output));
    assert_eq!(workspace.version("apps/b"), "1.0.1\n");
    assert_eq!(git(&workspace.root, &["tag"]), "v1.0.1");
}

#[test]
fn release_reloads_version_and_exclusions_after_pull() {
    let workspace = Workspace::new(&["apps/a", "apps/b"], "version = \"1.0.0\"\n");
    let upstream = workspace.temp.path().join("upstream");
    git(
        workspace.temp.path(),
        &[
            "clone",
            "--quiet",
            workspace
                .temp
                .path()
                .join("workspace.git")
                .to_str()
                .unwrap(),
            upstream.to_str().unwrap(),
        ],
    );
    identity(&upstream);
    std::fs::write(
        upstream.join("scripts/funbuild.toml"),
        "version = \"2.0.0\"\nexclude-apps = [\"b\"]\n",
    )
    .unwrap();
    commit(&upstream);
    git(&upstream, &["push", "--quiet"]);
    let output = workspace.run(&["build", "chore: test"]);
    assert!(output.status.success(), "{}", output_text(&output));
    assert_eq!(workspace.version("apps/a"), "2.0.1\n");
    assert_eq!(workspace.version("apps/b"), "1.0.0\n");
    assert_eq!(git(&workspace.root, &["tag"]), "v2.0.1");
}

#[test]
fn workspace_push_failure_does_not_report_apps_as_failed() {
    let workspace = Workspace::new(&["apps/a"], "version = \"1.0.0\"\n");
    git(
        &workspace.root,
        &[
            "remote",
            "set-url",
            "--push",
            "origin",
            "/nonexistent-funbuild-test-remote",
        ],
    );
    let output = workspace.run(&["build", "chore: test"]);
    let text = output_text(&output);
    assert!(!output.status.success(), "{text}");
    assert!(text.contains("apps/a: 成功"), "{text}");
    assert!(text.contains("工作区提交、推送或打标签失败"), "{text}");
}

#[test]
fn workspace_flags_are_rejected_by_other_builders_without_changes() {
    let dir = tempfile::tempdir().unwrap();
    git(dir.path(), &["init", "--quiet", "--initial-branch=main"]);
    std::fs::write(dir.path().join("VERSION"), "1.0.0\n").unwrap();
    for args in [vec!["build", "--dry-run"], vec!["build", "--apps", "a"]] {
        let output = command(env!("CARGO_BIN_EXE_funbuild"), dir.path())
            .args(args)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(output_text(&output).contains("only supported for submodule workspaces"));
        assert_eq!(
            std::fs::read_to_string(dir.path().join("VERSION")).unwrap(),
            "1.0.0\n"
        );
    }
}
