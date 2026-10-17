use std::path::Path;

use serde_norway::Value;

use crate::builders::{Builder, RepoContext};
use crate::error::{self, Result};
use crate::manifest::{pubspec, sync};
use crate::shell;
use crate::util::safe_clean_dir;

/// funpub 上传到的私有通用仓库名；凭据由使用方用 funsecret 预先配置，这里不经手。
const FUNPUB_REPO_NAME: &str = "funpackage";

const APK_PATH: &str = "build/app/outputs/flutter-apk/app-release.apk";
const WEB_DIR: &str = "build/web";
const WEB_ZIP: &str = "build/web-release.zip";

/// 基于 pubspec.yaml 的 Flutter 项目。
///
/// 版本号沿用 `major.minor.patch+buildNumber` 惯例：主版本走和其它构建类型相同的
/// 128 进制自增，buildNumber 每次 upgrade 单独 +1（应用商店要求它严格递增，不能
/// 随主版本号一起被当成后缀丢弃）。
pub struct FlutterBuild {
    ctx: RepoContext,
    build_number: Option<u64>,
    cfg: Value,
    pubspec_name: String,
}

fn as_command_list(value: Option<&Value>) -> Option<Vec<String>> {
    match value {
        Some(Value::String(text)) if !text.trim().is_empty() => Some(vec![text.trim().to_string()]),
        Some(Value::Sequence(items)) => {
            let commands: Vec<String> = items
                .iter()
                .filter_map(|item| item.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect();
            (!commands.is_empty()).then_some(commands)
        }
        _ => None,
    }
}

impl FlutterBuild {
    const PUBSPEC: &'static str = "pubspec.yaml";

    /// 必须显式依赖 Flutter SDK——纯 Dart 包该走 `dart pub` 工具链，不在本类范围内。
    pub fn detect(repo_path: &Path) -> Result<Option<Self>> {
        let path = repo_path.join(Self::PUBSPEC);
        if !path.is_file() {
            return Ok(None);
        }
        let data = match pubspec::load(&path) {
            Ok(data) => data,
            Err(error) => {
                log::warn!("skip pubspec.yaml, cannot parse: {error}");
                return Ok(None);
            }
        };
        if !data.is_mapping() || !pubspec::uses_flutter_sdk(&data) {
            return Ok(None);
        }
        let Some(raw_version) = pubspec::str_field(&data, "version") else {
            return Ok(None);
        };

        let (main, build) = match raw_version.split_once('+') {
            Some((main, build)) => (main.trim().to_string(), build.parse::<u64>().ok()),
            None => (raw_version, None),
        };
        let mut ctx = RepoContext::new(repo_path.to_path_buf());
        ctx.version = Some(main);
        let pubspec_name = pubspec::str_field(&data, "name").unwrap_or_else(|| ctx.name.clone());
        let cfg = data.get("funbuild").cloned().unwrap_or(Value::Null);
        Ok(Some(Self {
            ctx,
            build_number: build,
            cfg,
            pubspec_name,
        }))
    }

    fn cfg_field(&self, key: &str) -> Option<&Value> {
        self.cfg.get(key)
    }

    /// 项目若用 fvm 锁定了 Flutter 版本，把该 SDK 的 bin 目录临时加到 PATH 最前面。
    ///
    /// 命令链由 run_checked 串在同一个 shell 进程里，所以 export 对后续命令天然生效。
    fn fvm_path_prefix(&self) -> Vec<String> {
        let bin = self.ctx.repo_path.join(".fvm/flutter_sdk/bin");
        if bin.is_dir() {
            return vec![format!(
                "export PATH={}:\"$PATH\"",
                shell::quote(&bin.display().to_string())
            )];
        }
        Vec::new()
    }
}

impl Builder for FlutterBuild {
    fn ctx(&self) -> &RepoContext {
        &self.ctx
    }

    fn ctx_mut(&mut self) -> &mut RepoContext {
        &mut self.ctx
    }

    fn kind(&self) -> &'static str {
        "FlutterBuild"
    }

    fn write_version(&mut self) -> Result<()> {
        let version = self.ctx.version.clone().unwrap_or_default();
        let path = self.ctx.repo_path.join(Self::PUBSPEC);
        let raw = error::read_to_string(&path)?;
        let next_build = self.build_number.map(|n| n + 1);
        let token = match next_build {
            Some(build) => format!("{version}+{build}"),
            None => version.clone(),
        };
        let (updated, count) = pubspec::replace_version_line(&raw, |_| token.clone());
        if count == 0 {
            log::warn!("{} 未找到 version 字段, 跳过写入", path.display());
            return Ok(());
        }
        error::write(&path, updated)?;
        self.build_number = next_build;
        sync::sync_all(&self.ctx.repo_path, &version)
    }

    fn cmd_build(&self) -> Result<Vec<String>> {
        let mut result = self.fvm_path_prefix();
        result.push("flutter pub get".to_string());
        match as_command_list(self.cfg_field("build")) {
            Some(custom) => result.extend(custom),
            None => result.extend([
                "flutter build apk --release".to_string(),
                "flutter build web --release".to_string(),
            ]),
        }
        Ok(result)
    }

    fn cmd_install(&self) -> Result<Vec<String>> {
        Ok(as_command_list(self.cfg_field("install")).unwrap_or_default())
    }

    /// 默认经 funpub 上传到私有通用仓库。apk 单文件直传；web 产物是目录，先在子 shell
    /// 里打包成 zip 再传，避免 cd 通过 ` && ` 泄漏到后续命令。
    fn cmd_publish(&self) -> Result<Vec<String>> {
        if let Some(custom) = as_command_list(self.cfg_field("publish")) {
            return Ok(custom);
        }
        let version = shell::quote(self.ctx.version.as_deref().unwrap_or_default());
        let zip_name = Path::new(WEB_ZIP).file_name().unwrap().to_string_lossy();
        let zip_from_web_dir = format!("../{zip_name}");
        let name = &self.pubspec_name;
        Ok(vec![
            format!(
                "(cd {} && zip -rq {} .)",
                shell::quote(WEB_DIR),
                shell::quote(&zip_from_web_dir)
            ),
            format!(
                "funpub upload {} flutter/{name}/apk --version {version} --repo-name {FUNPUB_REPO_NAME}",
                shell::quote(APK_PATH)
            ),
            format!(
                "funpub upload {} flutter/{name}/web --version {version} --repo-name {FUNPUB_REPO_NAME}",
                shell::quote(WEB_ZIP)
            ),
        ])
    }

    fn cmd_delete(&self) -> Result<Vec<String>> {
        let mut result = self.fvm_path_prefix();
        if let Some(Value::Sequence(dirs)) = self.cfg_field("cleanDirs") {
            if !dirs.is_empty() {
                result.extend(
                    dirs.iter()
                        .filter_map(|d| d.as_str())
                        .filter_map(|d| safe_clean_dir(d, "pubspec.yaml funbuild.cleanDirs"))
                        .map(|d| format!("rm -rf {d}")),
                );
                return Ok(result);
            }
        }
        result.push("flutter clean".to_string());
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLUTTER_APP: &str =
        "name: demo_app\nversion: 1.0.0+7\ndependencies:\n  flutter:\n    sdk: flutter\n";

    fn pubspec(dir: &Path, body: &str) {
        std::fs::write(dir.join("pubspec.yaml"), body).unwrap();
    }

    #[test]
    fn detects_flutter_app_and_splits_build_number() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(dir.path(), FLUTTER_APP);
        let build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.ctx().version.as_deref(), Some("1.0.0"));
        assert_eq!(build.build_number, Some(7));
        assert_eq!(build.pubspec_name, "demo_app");
    }

    #[test]
    fn plain_dart_package_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(
            dir.path(),
            "name: demo\nversion: 1.0.0\ndependencies:\n  path: ^1.0.0\n",
        );
        assert!(FlutterBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn broken_pubspec_skips_without_breaking_the_chain() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(dir.path(), "name: [unclosed\n");
        assert!(FlutterBuild::detect(dir.path()).unwrap().is_none());
    }

    #[test]
    fn upgrade_bumps_both_version_and_build_number() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(dir.path(), FLUTTER_APP);
        let mut build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pubspec.yaml")).unwrap();
        assert!(body.contains("version: 1.0.1+8"), "{body}");
    }

    #[test]
    fn version_without_build_number_stays_without_one() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(
            dir.path(),
            "name: demo\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n",
        );
        let mut build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        build.upgrade(None).unwrap();
        let body = std::fs::read_to_string(dir.path().join("pubspec.yaml")).unwrap();
        assert!(body.contains("version: 1.0.1\n"), "{body}");
        assert!(!body.contains('+'), "{body}");
    }

    #[test]
    fn default_commands() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(dir.path(), FLUTTER_APP);
        let build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.cmd_build().unwrap(),
            vec![
                "flutter pub get",
                "flutter build apk --release",
                "flutter build web --release"
            ]
        );
        assert!(build.cmd_install().unwrap().is_empty());
        assert_eq!(build.cmd_delete().unwrap(), vec!["flutter clean"]);

        let publish = build.cmd_publish().unwrap();
        assert_eq!(publish[0], "(cd build/web && zip -rq ../web-release.zip .)");
        assert!(
            publish[1].contains("flutter/demo_app/apk"),
            "{:?}",
            publish[1]
        );
        assert!(publish[1].contains("--version 1.0.0"), "{:?}", publish[1]);
        assert!(
            publish[2].contains("flutter/demo_app/web"),
            "{:?}",
            publish[2]
        );
        assert!(
            publish.iter().all(|c| !c.contains("funpackage "))
                || publish[1].contains("--repo-name funpackage")
        );
    }

    #[test]
    fn config_overrides_each_stage() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(
            dir.path(),
            "name: demo\nversion: 1.0.0+1\ndependencies:\n  flutter:\n    sdk: flutter\nfunbuild:\n  build:\n    - flutter build apk\n    - flutter build appbundle\n  install: flutter install\n  publish: ./upload.sh\n  cleanDirs: [build, .dart_tool]\n",
        );
        let build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(
            build.cmd_build().unwrap(),
            vec![
                "flutter pub get",
                "flutter build apk",
                "flutter build appbundle"
            ]
        );
        assert_eq!(build.cmd_install().unwrap(), vec!["flutter install"]);
        assert_eq!(build.cmd_publish().unwrap(), vec!["./upload.sh"]);
        assert_eq!(
            build.cmd_delete().unwrap(),
            vec!["rm -rf build", "rm -rf .dart_tool"]
        );
    }

    #[test]
    fn clean_dirs_are_validated() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(
            dir.path(),
            "name: demo\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\nfunbuild:\n  cleanDirs: [build, \"/etc\", \"x; rm -rf /\"]\n",
        );
        let build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        assert_eq!(build.cmd_delete().unwrap(), vec!["rm -rf build"]);
    }

    #[test]
    fn fvm_sdk_is_prepended_to_path() {
        let dir = tempfile::tempdir().unwrap();
        pubspec(dir.path(), FLUTTER_APP);
        std::fs::create_dir_all(dir.path().join(".fvm/flutter_sdk/bin")).unwrap();
        let build = FlutterBuild::detect(dir.path()).unwrap().unwrap();
        let commands = build.cmd_build().unwrap();
        assert!(commands[0].starts_with("export PATH="), "{commands:?}");
        assert!(commands[0].contains(".fvm/flutter_sdk/bin"), "{commands:?}");
        // 清理命令同样要带上，否则 flutter clean 可能解析到另一个 SDK
        assert!(build.cmd_delete().unwrap()[0].starts_with("export PATH="));
    }
}
