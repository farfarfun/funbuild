import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from funbuild.core.submodule_workspace_build import SubmoduleWorkspaceBuild


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def new_builder(repo_path: str) -> SubmoduleWorkspaceBuild:
    builder = SubmoduleWorkspaceBuild.__new__(SubmoduleWorkspaceBuild)
    builder.repo_path = str(repo_path)
    builder.name = Path(repo_path).name
    builder.version = None
    return builder


class CheckTypeTest(unittest.TestCase):
    def test_matches_apps_dir_with_config(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "apps").mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('version = "1.2.3"\npackages = ["funtrack"]\n')

            builder = new_builder(repo)
            self.assertTrue(builder.check_type())
            self.assertEqual(builder.version, "1.2.3")

    def test_does_not_match_without_apps_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('version = "1.2.3"\n')

            builder = new_builder(repo)
            self.assertFalse(builder.check_type())

    def test_does_not_match_without_config_file(self):
        """向后兼容: 只有 apps/ 目录不足以命中, 避免误伤既有项目结构里同名目录。"""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "apps").mkdir()

            builder = new_builder(repo)
            self.assertFalse(builder.check_type())


class PinnedPackagesTest(unittest.TestCase):
    def test_pinned_packages_from_config(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text(
                'version = "1.0.0"\npackages = ["funtrack", "funlesson-core"]\n'
            )
            builder = new_builder(repo)
            self.assertEqual(builder._pinned_packages(), ["funtrack", "funlesson-core"])

    def test_dependency_names_uv(self):
        with tempfile.TemporaryDirectory() as temp:
            pyproject = Path(temp) / "pyproject.toml"
            pyproject.write_text('[project]\ndependencies = ["funtrack>=1.0.0", "Requests>=2.0"]\n')
            builder = new_builder(temp)
            self.assertEqual(builder._dependency_names_uv(str(pyproject)), {"funtrack", "requests"})

    def test_dependency_names_npm(self):
        with tempfile.TemporaryDirectory() as temp:
            package_json = Path(temp) / "package.json"
            package_json.write_text('{"dependencies": {"FunTrack": "^1.0.0"}, "devDependencies": {"eslint": "^8.0.0"}}')
            builder = new_builder(temp)
            self.assertEqual(builder._dependency_names_npm(str(package_json)), {"funtrack", "eslint"})

    def test_dependency_names_flutter(self):
        with tempfile.TemporaryDirectory() as temp:
            pubspec = Path(temp) / "pubspec.yaml"
            pubspec.write_text(
                "dependencies:\n"
                "  flutter:\n"
                "    sdk: flutter\n"
                "  funtrack: ^1.0.0\n"
                "dev_dependencies:\n"
                "  flutter_test:\n"
                "    sdk: flutter\n"
            )
            builder = new_builder(temp)
            self.assertEqual(builder._dependency_names_flutter(str(pubspec)), {"funtrack"})


class IsNestedWorkspaceTest(unittest.TestCase):
    def test_nested_workspace_detected_structurally(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "apps").mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "setup.sh").write_text("#!/bin/sh\n")
            builder = new_builder(repo)
            self.assertTrue(builder._is_nested_workspace(str(repo)))

    def test_plain_app_is_not_nested_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = new_builder(temp)
            self.assertFalse(builder._is_nested_workspace(temp))


class UpgradePinnedPackagesTest(unittest.TestCase):
    """依赖升级委托给各生态自己的 CLI, 这里只验证按 manifest 类型正确分派命令,
    不真的联网升级。"""

    def test_upgrades_matching_uv_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "workspace"
            repo.mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('packages = ["funtrack"]\n')
            app = Path(temp) / "app"
            app.mkdir()
            (app / "pyproject.toml").write_text('[project]\ndependencies = ["funtrack>=1.0.0"]\n')

            builder = new_builder(repo)
            with patch("funbuild.core.submodule_workspace_build.run_checked") as mock_run:
                builder._upgrade_pinned_packages(str(app))
            mock_run.assert_has_calls(
                [
                    call(["uv remove funtrack", "uv add funtrack"], cwd=str(app)),
                ]
            )
            assert mock_run.call_count == 1

    def test_upgrades_matching_npm_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "workspace"
            repo.mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('packages = ["funtrack"]\n')
            app = Path(temp) / "app"
            app.mkdir()
            (app / "package.json").write_text('{"dependencies": {"funtrack": "^1.0.0"}}')

            builder = new_builder(repo)
            with patch("funbuild.core.submodule_workspace_build.run_checked") as mock_run:
                builder._upgrade_pinned_packages(str(app))
            mock_run.assert_called_once_with(["npm install funtrack@latest --save"], cwd=str(app))

    def test_upgrades_matching_flutter_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "workspace"
            repo.mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('packages = ["funtrack"]\n')
            app = Path(temp) / "app"
            app.mkdir()
            (app / "pubspec.yaml").write_text("dependencies:\n  flutter:\n    sdk: flutter\n  funtrack: ^1.0.0\n")

            builder = new_builder(repo)
            with patch("funbuild.core.submodule_workspace_build.run_checked") as mock_run:
                builder._upgrade_pinned_packages(str(app))
            mock_run.assert_called_once_with(["dart pub add funtrack"], cwd=str(app))

    def test_no_match_skips_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "workspace"
            repo.mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "funbuild.toml").write_text('packages = ["funtrack"]\n')
            app = Path(temp) / "app"
            app.mkdir()
            (app / "pyproject.toml").write_text('[project]\ndependencies = ["requests>=2.0"]\n')

            builder = new_builder(repo)
            with patch("funbuild.core.submodule_workspace_build.run_checked") as mock_run:
                builder._upgrade_pinned_packages(str(app))
            mock_run.assert_not_called()


class BuildIntegrationTest(unittest.TestCase):
    """`funbuild build` 在 workspace 根目录: 递增共享版本号, 对每个非嵌套 submodule
    转发 `funbuild build --version <共享版本号>`, 嵌套 workspace 原样跳过。"""

    def test_build_bumps_version_and_dispatches_to_apps(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)

            app_remote = temp_path / "app_remote.git"
            nested_remote = temp_path / "nested_remote.git"
            main_remote = temp_path / "main_remote.git"
            git(temp, "init", "--bare", str(app_remote))
            git(temp, "init", "--bare", str(nested_remote))
            git(temp, "init", "--bare", str(main_remote))

            def init_repo(path):
                path.mkdir()
                git(path, "init")
                git(path, "config", "user.name", "Test")
                git(path, "config", "user.email", "test@example.com")

            app_src = temp_path / "app_src"
            init_repo(app_src)
            (app_src / "pyproject.toml").write_text("[project]\ndependencies = []\n")
            git(app_src, "add", "pyproject.toml")
            git(app_src, "commit", "-m", "app initial")
            git(app_src, "remote", "add", "origin", str(app_remote))
            git(app_src, "push", "-u", "origin", "HEAD")

            nested_src = temp_path / "nested_src"
            init_repo(nested_src)
            (nested_src / "apps").mkdir()
            # git 不追踪空目录, 必须放一个占位文件, 否则 clone 出来的 submodule
            # 工作区里根本没有 apps/ 目录, 会让 _is_nested_workspace() 误判为 False。
            (nested_src / "apps" / ".gitkeep").write_text("")
            (nested_src / "scripts").mkdir()
            (nested_src / "scripts" / "setup.sh").write_text("#!/bin/sh\n")
            git(nested_src, "add", "-A")
            git(nested_src, "commit", "-m", "nested initial")
            git(nested_src, "remote", "add", "origin", str(nested_remote))
            git(nested_src, "push", "-u", "origin", "HEAD")

            main_src = temp_path / "main_src"
            init_repo(main_src)
            (main_src / "scripts").mkdir()
            (main_src / "scripts" / "funbuild.toml").write_text('version = "1.0.0"\npackages = []\n')
            git(main_src, "add", "-A")
            git(main_src, "commit", "-m", "main initial")
            git(main_src, "remote", "add", "origin", str(main_remote))
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(app_remote), "apps/app"],
                cwd=main_src,
                check=True,
            )
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(nested_remote), "apps/nested-dev"],
                cwd=main_src,
                check=True,
            )
            git(main_src, "commit", "-m", "add submodules")
            git(main_src, "push", "-u", "origin", "HEAD")

            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            log_file = temp_path / "invocations.log"
            (fake_bin / "funbuild").write_text(
                "#!/bin/sh\n"
                f'echo "$PWD $*" >> "{log_file}"\n'
                'if [ "$1" = "build" ]; then\n'
                '  git commit --allow-empty -q -m "child build $3"\n'
                "  git push -q\n"
                "fi\n",
                encoding="utf-8",
            )
            (fake_bin / "funbuild").chmod(0o755)

            builder = new_builder(main_src)

            # tags() 用相对进程 cwd 的 shell 命令打 tag/push, 不是 cd 到 self.repo_path,
            # 必须真的 chdir 进去, 否则会在测试进程原本的工作目录 (真实的 funbuild 仓库)
            # 里打 tag、push 到它的真实 origin —— 这不是假设, 是本文件写作过程中
            # 真实炸过一次的教训, 千万别再删这行。
            original_cwd = os.getcwd()
            original_path = os.environ["PATH"]
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path}"
            os.chdir(main_src)
            try:
                with patch("funbuild.core.submodule_workspace_build.logger"):
                    builder.build(message="发布测试版本")
            finally:
                os.chdir(original_cwd)
                os.environ["PATH"] = original_path

            app_head = git(main_src / "apps" / "app", "rev-parse", "HEAD")
            self.assertEqual(app_head, git(app_remote, "rev-parse", "HEAD"))
            log_content = log_file.read_text() if log_file.exists() else ""
            self.assertIn(str(main_src / "apps" / "app"), log_content)
            self.assertIn(f"--version {builder.version}", log_content)

            nested_before = git(nested_src, "rev-parse", "HEAD")
            nested_after = git(main_src / "apps" / "nested-dev", "rev-parse", "HEAD")
            self.assertEqual(nested_before, nested_after)
            self.assertNotIn(str(main_src / "apps" / "nested-dev"), log_content)

            written_config = (main_src / "scripts" / "funbuild.toml").read_text()
            self.assertIn(builder.version, written_config)
            self.assertNotEqual(builder.version, "1.0.0")

            main_branch = git(main_src, "branch", "--show-current")
            self.assertEqual(
                git(main_src, "rev-parse", "HEAD"),
                git(main_remote, "rev-parse", main_branch),
            )


if __name__ == "__main__":
    unittest.main()
