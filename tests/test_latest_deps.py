"""`latest-packages`: 发版时把依赖下界抬到最新已发布版本。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from funbuild.core.base import BaseBuild, git_repo_root
from funbuild.core.latest_deps import (
    LatestDependencyError,
    latest_packages,
    resolve_latest_version,
    rewrite_requirement,
    sync_latest_dependencies,
)
from funbuild.core.uv_build import UVBuild


class RewriteRequirementTest(unittest.TestCase):
    """改写下界时, 调用方刻意写下的其它信息必须一字不动。"""

    def test_lower_bound_is_raised(self):
        self.assertEqual(rewrite_requirement("funflix>=1.0.0", "1.9.0"), "funflix>=1.9.0")

    def test_missing_constraint_gets_a_floor(self):
        self.assertEqual(rewrite_requirement("funflix", "1.9.0"), "funflix>=1.9.0")

    def test_extras_and_upper_bound_survive(self):
        self.assertEqual(rewrite_requirement("funflix[all]>=1.0,<2", "1.9.0"), "funflix[all]>=1.9.0,<2")

    def test_marker_survives(self):
        self.assertEqual(
            rewrite_requirement('funflix>=1.0; python_version>="3.11"', "1.9.0"),
            'funflix>=1.9.0; python_version>="3.11"',
        )

    def test_exclusion_is_kept(self):
        self.assertEqual(rewrite_requirement("funflix>=1.0,!=1.5.0", "1.9.0"), "funflix>=1.9.0,!=1.5.0")

    def test_exact_pin_follows_latest(self):
        """`==` 也是「从哪个版本起」的一种; 包既然进了名单, 就该跟着最新版走。"""
        self.assertEqual(rewrite_requirement("funflix==1.0.0", "1.9.0"), "funflix==1.9.0")

    def test_compatible_release_follows_latest(self):
        self.assertEqual(rewrite_requirement("funflix~=1.0", "1.9.0"), "funflix~=1.9.0")

    def test_direct_reference_is_left_alone(self):
        """`@ url` 指定的来源不是 index, 悄悄换掉等于改了依赖的出处。"""
        self.assertIsNone(rewrite_requirement("funflix @ https://example.com/funflix.whl", "1.9.0"))

    def test_already_latest_reports_no_change(self):
        self.assertIsNone(rewrite_requirement("funflix>=1.9.0", "1.9.0"))

    def test_parenthesized_form(self):
        self.assertEqual(rewrite_requirement("funflix (>=1.0)", "1.9.0"), "funflix>=1.9.0")

    def test_duplicate_lower_bounds_collapse(self):
        self.assertEqual(rewrite_requirement("funflix>=1.0,>=1.2", "1.9.0"), "funflix>=1.9.0")


class LatestPackagesConfigTest(unittest.TestCase):
    """配置来源: 独立仓库看 pyproject, 编排仓库沿用 scripts/funbuild.toml。"""

    def test_reads_pyproject_table(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "pyproject.toml").write_text(
                '[project]\nname = "funflix-api"\n\n[tool.funbuild]\nlatest-packages = ["funflix"]\n',
                encoding="utf-8",
            )
            self.assertEqual(latest_packages(temp), ["funflix"])

    def test_reads_workspace_config(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "scripts").mkdir()
            Path(temp, "scripts/funbuild.toml").write_text('version = "1.0.0"\npackages = ["funtrack"]\n', "utf-8")
            self.assertEqual(latest_packages(temp), ["funtrack"])

    def test_sources_are_merged_and_deduped(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "pyproject.toml").write_text('[tool.funbuild]\nlatest-packages = ["funflix"]\n', "utf-8")
            Path(temp, "scripts").mkdir()
            Path(temp, "scripts/funbuild.toml").write_text('packages = ["funflix", "funtrack"]\n', "utf-8")
            self.assertEqual(latest_packages(temp), ["funflix", "funtrack"])

    def test_no_config_is_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
            self.assertEqual(latest_packages(temp), [])


class ResolveLatestVersionTest(unittest.TestCase):
    def test_parses_pinned_line(self):
        with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n") as mock_run:
            self.assertEqual(resolve_latest_version("funflix", "/tmp"), "1.9.0")
        self.assertIn("uv pip compile", mock_run.call_args.args[0])
        self.assertIn("--no-deps", mock_run.call_args.args[0])

    def test_matches_normalized_name(self):
        with patch("funbuild.core.latest_deps.run_shell", return_value="fun-flix==1.9.0\n"):
            self.assertEqual(resolve_latest_version("Fun_Flix", "/tmp"), "1.9.0")

    def test_unresolvable_aborts(self):
        """解析不出来就必须中止, 不能沿用旧下界继续发布。"""
        with patch("funbuild.core.latest_deps.run_shell", return_value=""):
            with self.assertRaises(LatestDependencyError):
                resolve_latest_version("funflix", "/tmp")

    def test_other_packages_in_output_are_ignored(self):
        with patch("funbuild.core.latest_deps.run_shell", return_value="loguru==0.7.3\nfunflix==1.9.0\n"):
            self.assertEqual(resolve_latest_version("funflix", "/tmp"), "1.9.0")

    def test_temp_requirement_file_is_removed(self):
        seen: list[str] = []

        def capture(command, **kwargs):
            seen.append(command.split()[-1])
            return "funflix==1.9.0\n"

        with patch("funbuild.core.latest_deps.run_shell", side_effect=capture):
            resolve_latest_version("funflix", "/tmp")
        self.assertFalse(os.path.exists(seen[0]))


class SyncLatestDependenciesTest(unittest.TestCase):
    def test_no_config_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
            with patch("funbuild.core.latest_deps.run_shell") as mock_run:
                self.assertEqual(sync_latest_dependencies(temp, [str(Path(temp, "pyproject.toml"))]), [])
            mock_run.assert_not_called()

    def test_rewrites_all_dependency_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "pyproject.toml")
            path.write_text(
                "[project]\n"
                'name = "funflix-api"\n'
                'dependencies = ["funflix>=1.0.0", "requests>=2"]\n'
                "\n"
                "[project.optional-dependencies]\n"
                'web = ["funflix>=1.0.0"]\n'
                "\n"
                "[dependency-groups]\n"
                'dev = ["funflix>=1.0.0", {include-group = "web"}]\n'
                "\n"
                "[tool.funbuild]\n"
                'latest-packages = ["funflix"]\n',
                encoding="utf-8",
            )
            with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n"):
                changed = sync_latest_dependencies(temp, [str(path)])

            self.assertEqual(changed, [str(path)])
            config = tomlkit.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["project"]["dependencies"], ["funflix>=1.9.0", "requests>=2"])
            self.assertEqual(config["project"]["optional-dependencies"]["web"], ["funflix>=1.9.0"])
            self.assertEqual(config["dependency-groups"]["dev"][0], "funflix>=1.9.0")
            self.assertEqual(dict(config["dependency-groups"]["dev"][1]), {"include-group": "web"})

    def test_comments_and_layout_survive(self):
        """整仓都用 tomlkit 就是为了不污染 diff, 这一步不能破例。"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "pyproject.toml")
            raw = (
                "[project]\n"
                'name = "funflix-api"\n'
                "# 上游内核\n"
                "dependencies = [\n"
                '    "funflix>=1.0.0",  # 每次发版取最新\n'
                '    "requests>=2",\n'
                "]\n"
                "\n"
                "[tool.funbuild]\n"
                'latest-packages = ["funflix"]\n'
            )
            path.write_text(raw, encoding="utf-8")
            with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n"):
                sync_latest_dependencies(temp, [str(path)])
            updated = path.read_text(encoding="utf-8")
            self.assertIn("# 上游内核", updated)
            self.assertIn('"funflix>=1.9.0",  # 每次发版取最新', updated)

    def test_unlisted_dependency_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "pyproject.toml")
            path.write_text(
                '[project]\ndependencies = ["requests>=2"]\n\n[tool.funbuild]\nlatest-packages = ["funflix"]\n',
                encoding="utf-8",
            )
            with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n"):
                self.assertEqual(sync_latest_dependencies(temp, [str(path)]), [])
            self.assertIn('"requests>=2"', path.read_text(encoding="utf-8"))

    def test_already_latest_leaves_file_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "pyproject.toml")
            path.write_text(
                '[project]\ndependencies = ["funflix>=1.9.0"]\n\n[tool.funbuild]\nlatest-packages = ["funflix"]\n',
                encoding="utf-8",
            )
            before = path.stat().st_mtime_ns
            with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n"):
                self.assertEqual(sync_latest_dependencies(temp, [str(path)]), [])
            self.assertEqual(path.stat().st_mtime_ns, before)


class BuildPipelineHookTest(unittest.TestCase):
    """接线: 这一步必须跑在构建/发布之前, 否则改动进不了本次 metadata。"""

    def test_runs_before_build_commands(self):
        order: list[str] = []
        builder = BaseBuild.__new__(BaseBuild)
        builder.repo_path = "/tmp"
        builder.name = "repo"
        builder.version = "1.0.0"

        with (
            patch.object(BaseBuild, "pull"),
            patch.object(BaseBuild, "upgrade"),
            patch.object(BaseBuild, "push"),
            patch.object(BaseBuild, "tags"),
            patch.object(BaseBuild, "_sync_latest_dependencies", side_effect=lambda: order.append("sync")),
            patch("funbuild.core.base.run_checked", side_effect=lambda *a, **k: order.append("shell")),
        ):
            builder.build(message="chore: 测试发布流程")
        self.assertEqual(order, ["sync", "shell"])

    def test_default_hook_is_a_noop(self):
        builder = BaseBuild.__new__(BaseBuild)
        builder.repo_path = "/tmp"
        builder.name = "repo"
        builder.version = "1.0.0"
        self.assertIsNone(builder._sync_latest_dependencies())

    def test_uv_build_covers_subpackages(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp, "funflix-api")
            repo.mkdir()
            (repo / "pyproject.toml").write_text(
                "[project]\n"
                'name = "funflix-api"\n'
                'version = "1.0.0"\n'
                'dependencies = ["funflix>=1.0.0"]\n'
                "\n"
                "[tool.funbuild]\n"
                'latest-packages = ["funflix"]\n',
                encoding="utf-8",
            )
            sub = repo / "extbuild" / "plugin"
            sub.mkdir(parents=True)
            (sub / "pyproject.toml").write_text(
                '[project]\nname = "funflix-plugin"\nversion = "1.0.0"\ndependencies = ["funflix>=1.0.0"]\n',
                encoding="utf-8",
            )

            cwd = os.getcwd()
            os.chdir(repo)
            git_repo_root.cache_clear()
            try:
                with patch("funbuild.core.base.run_shell", return_value=str(repo)):
                    build = UVBuild()
                with patch("funbuild.core.latest_deps.run_shell", return_value="funflix==1.9.0\n"):
                    build._sync_latest_dependencies()
            finally:
                os.chdir(cwd)
                git_repo_root.cache_clear()

            self.assertIn('"funflix>=1.9.0"', (repo / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertIn('"funflix>=1.9.0"', (sub / "pyproject.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
