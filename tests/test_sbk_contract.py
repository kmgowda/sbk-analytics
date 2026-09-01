import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from analytics.config import load_config
from analytics.policy import RUNTIME_POLICY
from analytics.releases import ensure_sbk
from analytics.runner import RunResult, _sbk_env, _terminate_sbk_process
from analytics.yaml_gen import generate_instance_yaml


class SbkContractConfigurationTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.yml"
            path.write_text(content, encoding="utf-8")
            return load_config(path)

    def test_runtimecleanup_is_migrated_to_packagescleanup(self):
        config = self._load(
            "classes: [file]\nsbk:\n  nodes: [node1]\n  runtimecleanup: true\n"
        )
        params = config.instances[0].params
        self.assertNotIn("runtimecleanup", params)
        self.assertIs(params["packagescleanup"], True)

    def test_removed_gem_deployment_options_are_rejected(self):
        for option in (
            "copyonlydrivers", "compactruntimecopy", "compactcopy", "copy",
            "deleteafter", "delete", "sbkcommand", "sbkdir", "javacopy",
            "javaversion",
        ):
            with self.subTest(option=option), self.assertRaisesRegex(
                ValueError, "SBK removed option"
            ):
                self._load(
                    f"classes: [file]\nsbk:\n  nodes: [node1]\n  {option}: true\n"
                )

    def test_new_gem_options_and_wrapper_are_preserved(self):
        config = self._load(
            "classes: [file]\nsbk:\n"
            "  nodes: [node1, node2]\n"
            "  packagescleanup: true\n"
            "  fullcopy: false\n"
            "  hostkeycheck: true\n"
            "  knownhosts: /tmp/known_hosts\n"
            "  sbmport: 9719\n"
            "  sbmsleepms: 1\n"
            "  totalrecords: 1000\n"
            "  idletimeoutseconds: 600\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = generate_instance_yaml(
                config.instances[0], root, root / "result.csv"
            )
            document = yaml.safe_load(generated.read_text())
        params = document["sbkGemArgs"]
        self.assertEqual(params["nodes"], "node1,node2")
        self.assertEqual(params["sbmport"], 9719)
        self.assertEqual(params["idletimeoutseconds"], 600)

    def test_gem_only_options_require_nodes(self):
        with self.assertRaisesRegex(ValueError, "require a non-empty 'nodes'"):
            self._load("classes: [file]\nsbk:\n  packagescleanup: true\n")

    def test_aggregate_option_conflicts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            self._load(
                "classes: [file]\nsbk:\n  nodes: node1\n"
                "  totalrecords: 100\n  records: 10\n"
            )

    def test_blank_nodes_does_not_enable_gem_options(self):
        with self.assertRaisesRegex(ValueError, "require a non-empty 'nodes'"):
            self._load(
                "classes: [file]\nsbk:\n  nodes: '   '\n  fullcopy: false\n"
            )

    def test_contract_numeric_options_are_validated(self):
        for option in ("idletimeoutseconds", "gemport", "sbmport", "totalrecords"):
            nodes = "  nodes: node1\n" if option != "idletimeoutseconds" else ""
            with self.subTest(option=option), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                self._load(
                    f"classes: [file]\nsbk:\n{nodes}  {option}: 0\n"
                )
        with self.assertRaisesRegex(ValueError, "positive number"):
            self._load(
                "classes: [file]\nsbk:\n  nodes: node1\n  totalthroughput: -1\n"
            )
        config = self._load(
            "classes: [file]\nsbk:\n  nodes: node1\n  sbmsleepms: 0\n"
        )
        self.assertEqual(config.instances[0].params["sbmsleepms"], 0)


class SbkContractResolutionTests(unittest.TestCase):
    def test_managed_cache_reports_configured_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = "11.0"
            cache = root / version
            home = cache / "extracted" / "sbk"
            (home / "bin").mkdir(parents=True)
            (home / "bin" / "sbk-yal").touch()
            (cache / ".home").write_text(str(home), encoding="utf-8")
            (cache / ".ok").touch()
            install = ensure_sbk(version, downloads_folder=root)
        self.assertEqual(install.detected_version, version)


class SbkContractLifecycleTests(unittest.TestCase):
    def test_jdk_environment_is_built_once_without_mutating_parent(self):
        java_home = RUNTIME_POLICY.environment.java_home
        sbk_java_home = RUNTIME_POLICY.environment.sbk_java_home
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {java_home: "/different/java", sbk_java_home: "/parent/java"},
            clear=False,
        ):
            before = os.environ.copy()
            child = _sbk_env(Path(directory))
            self.assertEqual(child[sbk_java_home], directory)
            self.assertNotIn(java_home, child)
            self.assertEqual(os.environ, before)

    def test_nonzero_exit_is_failure_even_with_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "partial.csv"
            csv.write_text("header\nrow\n", encoding="utf-8")
            result = RunResult("file", Path("job.yml"), csv, None, 1, 0.1)
            self.assertFalse(result.ok)

    def test_gem_interrupt_allows_native_cleanup_first(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 143
        _terminate_sbk_process(process, Path("job.yml"), is_gem=True)
        process.terminate.assert_called_once_with()

    def test_gem_interrupt_force_stays_scoped_to_local_owned_tree(self):
        process = mock.Mock()
        process.poll.return_value = None
        # subprocess.TimeoutExpired is required by the production handler.
        import subprocess
        process.wait.side_effect = subprocess.TimeoutExpired("gem", 30)
        with mock.patch(
            "analytics.runner.terminate_process", return_value=0
        ) as terminate:
            _terminate_sbk_process(process, Path("job.yml"), is_gem=True)
        terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
