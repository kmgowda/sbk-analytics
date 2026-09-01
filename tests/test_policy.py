import ast
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analytics.cli import _bundled_versions_file
from analytics.policy import (
    APPLICATION,
    ARTIFACTS,
    JDK_ARTIFACT,
    RUNTIME_POLICY,
    SBK_ARTIFACT,
    SBK_CHARTS_ARTIFACT,
)
from analytics.properties import parse_properties


ROOT = Path(__file__).resolve().parent.parent


class PolicyTests(unittest.TestCase):
    @staticmethod
    def _bootstrap_policy() -> dict[str, str]:
        values = {}
        for raw in (ROOT / "sbk-bootstrap.env").read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key] = value
        return values

    def test_runtime_policy_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RUNTIME_POLICY.processes.termination_grace_s = 9

    def test_artifact_registry_has_unique_complete_metadata(self):
        self.assertEqual(set(ARTIFACTS), {"sbk", "sbk-charts", "jdk"})
        self.assertEqual(len({item.cache_namespace for item in ARTIFACTS.values()}), 3)
        for key, artifact in ARTIFACTS.items():
            self.assertEqual(key, artifact.key)
            self.assertTrue(artifact.repository_url.startswith("https://"))
            self.assertTrue(artifact.primary_executable)

    def test_application_metadata_is_complete(self):
        self.assertEqual(APPLICATION.command_name, "sbk-analytics")
        self.assertEqual(APPLICATION.distribution_name, "sbk-analytics")
        self.assertTrue(APPLICATION.repository_url.startswith("https://"))
        project_metadata = (ROOT / "pyproject.toml").read_text()
        self.assertIn(
            f'name = "{APPLICATION.distribution_name}"',
            project_metadata,
        )
        self.assertIn(APPLICATION.repository_url, project_metadata)

    def test_launcher_source_root_preserves_checkout_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            config = source / "sbk-config.env"
            config.write_text("sbk.version=local\n")
            with mock.patch.dict(
                os.environ, {"SBK_ANALYTICS_SOURCE_ROOT": str(source)}
            ):
                self.assertEqual(_bundled_versions_file(), config)

    def test_expected_artifact_executables_are_centralized(self):
        self.assertEqual(SBK_ARTIFACT.executables, ("sbk-yal", "sbk-gem-yal"))
        self.assertEqual(SBK_CHARTS_ARTIFACT.executables, ("sbk-charts",))
        self.assertEqual(JDK_ARTIFACT.executables, ("java",))
        self.assertEqual(
            JDK_ARTIFACT.repository_url,
            "https://github.com/adoptium/temurin-binaries",
        )
        self.assertIn("{version}", JDK_ARTIFACT.metadata_url_template)

    def test_platform_paths_come_from_the_host_runtime(self):
        self.assertEqual(RUNTIME_POLICY.ssh.known_hosts_file, os.devnull)
        self.assertEqual(
            RUNTIME_POLICY.configuration.default_workdir,
            os.path.join(tempfile.gettempdir(), APPLICATION.name),
        )

    def test_lifecycle_registry_follows_application_state_override(self):
        from analytics.lifecycle import registry_root

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                RUNTIME_POLICY.environment.application_state_home: directory,
                RUNTIME_POLICY.environment.lifecycle_folder: "",
            },
        ):
            self.assertEqual(
                registry_root(),
                Path(directory) / RUNTIME_POLICY.lifecycle.registry_directory,
            )

    def test_runtime_ordering_constraints_are_valid(self):
        benchmark = RUNTIME_POLICY.benchmarks
        self.assertGreater(benchmark.gem_native_shutdown_grace_s, 0)
        self.assertGreater(RUNTIME_POLICY.processes.termination_grace_s, 0)
        self.assertGreater(RUNTIME_POLICY.lifecycle.schema_version, 0)
        self.assertGreater(RUNTIME_POLICY.lifecycle.identity_tolerance_s, 0)
        self.assertGreater(RUNTIME_POLICY.network.artifact_download_attempts, 0)
        self.assertGreater(
            RUNTIME_POLICY.dependencies.source_control_timeout_s, 0
        )
        self.assertGreater(RUNTIME_POLICY.display.bytes_per_kibibyte, 0)
        self.assertGreater(RUNTIME_POLICY.exit_codes.signal_base, 0)

    def test_dependency_runtime_vocabulary_is_centralized(self):
        provenance = RUNTIME_POLICY.provenance
        self.assertNotEqual(
            provenance.shared_folder_mode,
            provenance.github_release_mode,
        )
        self.assertEqual(
            set(SBK_ARTIFACT.executables),
            {"sbk-yal", "sbk-gem-yal"},
        )
        self.assertEqual(
            RUNTIME_POLICY.dependency_layout.sbk_gradle_install_path,
            ("build", "install", "sbk"),
        )
        self.assertEqual(
            RUNTIME_POLICY.sbk_interface.nodes_option,
            "nodes",
        )

    def test_shipped_configuration_matches_canonical_metadata(self):
        root_config = parse_properties(ROOT / "sbk-config.env")
        bundled_config = parse_properties(
            ROOT / "analytics" / "default-sbk-config.env"
        )
        for config in (root_config, bundled_config):
            self.assertEqual(config.sbk_url, SBK_ARTIFACT.repository_url)
            self.assertEqual(
                config.sbk_charts_url,
                SBK_CHARTS_ARTIFACT.repository_url,
            )
            self.assertEqual(
                config.sbk_jdk,
                RUNTIME_POLICY.dependencies.default_jdk_version,
            )
            self.assertEqual(
                config.jdk_folder.name,
                Path(RUNTIME_POLICY.cache.default_jdk_folder).name,
            )
            self.assertEqual(
                config.downloads_folder.name,
                Path(RUNTIME_POLICY.cache.default_downloads_folder).name,
            )
            self.assertEqual(
                config.ssl_verify,
                RUNTIME_POLICY.dependencies.default_ssl_verify,
            )
            self.assertRegex(config.sbk_charts_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(root_config.sbk, bundled_config.sbk)
        self.assertEqual(root_config.sbk_charts, bundled_config.sbk_charts)
        self.assertEqual(
            root_config.sbk_charts_sha256,
            bundled_config.sbk_charts_sha256,
        )

    def test_bootstrap_policy_matches_packaging_metadata(self):
        bootstrap = self._bootstrap_policy()
        self.assertIn(
            'requires-python = ">=3.9"',
            (ROOT / "pyproject.toml").read_text(),
        )
        self.assertEqual(
            (ROOT / ".python-version").read_text().strip(),
            bootstrap["SBK_ANALYTICS_PYTHON_VERSION"],
        )
        self.assertRegex(bootstrap["SBK_ANALYTICS_UV_VERSION"], r"^\d+\.\d+\.\d+$")
        checksum_keys = [key for key in bootstrap if key.endswith("_SHA256")]
        self.assertEqual(len(checksum_keys), 4)
        self.assertFalse(any("WINDOWS" in key for key in bootstrap))
        self.assertFalse((ROOT / "sbk-analytics.ps1").exists())
        for key in checksum_keys:
            self.assertRegex(bootstrap[key], r"^[0-9a-f]{64}$")
        launcher_text = (ROOT / "sbk-analytics.sh").read_text()
        self.assertIn("sbk-bootstrap.env", launcher_text)
        for key in (
            "SBK_ANALYTICS_PYTHON_VERSION",
            "SBK_ANALYTICS_UV_VERSION",
            "SBK_ANALYTICS_UV_RELEASE_BASE",
            "SBK_ANALYTICS_RUNTIME_FOLDER",
            "SBK_ANALYTICS_BOOTSTRAP_MARKER",
            "SBK_ANALYTICS_ENV_METADATA",
            "SBK_ANALYTICS_ENV_METADATA_SCHEMA",
            "SBK_ANALYTICS_LOCK_ATTEMPTS",
            "SBK_ANALYTICS_LOCK_POLL_SECONDS",
        ):
            self.assertIn(key, launcher_text)
        for key in checksum_keys:
            self.assertIn(key, launcher_text)
        manifest = (ROOT / "MANIFEST.in").read_text()
        for filename in (
            "sbk-bootstrap.env",
            "sbk-analytics",
            "sbk-analytics.sh",
            ".python-version",
            "uv.lock",
        ):
            self.assertIn(f"include {filename}", manifest)
        self.assertNotIn("sbk-analytics.ps1", manifest)

    def test_policy_consumers_do_not_reintroduce_cross_cutting_literals(self):
        consumers = (
            "cli.py",
            "config.py",
            "lifecycle.py",
            "processes.py",
            "properties.py",
            "releases.py",
            "runner.py",
            "system_info.py",
        )
        forbidden_strings = {
            ".ok",
            ".home",
            "metadata.json",
            SBK_ARTIFACT.repository_url,
            SBK_CHARTS_ARTIFACT.repository_url,
            RUNTIME_POLICY.provenance.shared_folder_mode,
            RUNTIME_POLICY.provenance.github_release_mode,
            RUNTIME_POLICY.provenance.gradle_install_layout,
            RUNTIME_POLICY.provenance.source_launcher_layout,
            RUNTIME_POLICY.provenance.explicit_executable_layout,
            RUNTIME_POLICY.environment.sbk_java_home,
            RUNTIME_POLICY.environment.java_tool_options,
            RUNTIME_POLICY.environment.lifecycle_run_id,
            RUNTIME_POLICY.sbk_interface.local_arguments_wrapper,
            RUNTIME_POLICY.sbk_interface.gem_arguments_wrapper,
        }
        violations = []
        for filename in consumers:
            tree = ast.parse((ROOT / "analytics" / filename).read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in forbidden_strings
                ):
                    violations.append(f"{filename}:{node.lineno}: {node.value}")
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if (
                        keyword.arg == "timeout"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, (int, float))
                    ):
                        violations.append(
                            f"{filename}:{node.lineno}: numeric timeout"
                        )
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sleep"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, (int, float))
                ):
                    violations.append(f"{filename}:{node.lineno}: numeric sleep")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
