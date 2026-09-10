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


class SbkPassThroughConfigurationTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.yml"
            path.write_text(content, encoding="utf-8")
            return load_config(path)

    def test_runtimecleanup_is_left_for_sbk_to_validate(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n  nodes: [node1]\n  runtimecleanup: true\n"
        )
        params = config.instances[0].params
        self.assertIs(params["runtimecleanup"], True)
        self.assertNotIn("packagescleanup", params)

    def test_unknown_and_legacy_sbk_options_are_preserved(self):
        for option in (
            "copyonlydrivers", "compactruntimecopy", "compactcopy", "copy",
            "deleteafter", "delete", "sbkcommand", "sbkdir", "javacopy",
            "javaversion",
        ):
            with self.subTest(option=option):
                config = self._load(
                    "benchmarks:\n  file-run:\n    file: {}\n"
                    f"sbk:\n  nodes: [node1]\n  {option}: true\n"
                )
                self.assertIs(config.instances[0].params[option], True)

    def test_new_gem_options_and_wrapper_are_preserved(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n"
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

    def test_sbk_decides_whether_an_option_requires_gem(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n  packagescleanup: true\n"
        )
        self.assertIs(config.instances[0].params["packagescleanup"], True)

    def test_sbk_decides_option_conflicts(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n  nodes: node1\n"
            "  totalrecords: 100\n  records: 10\n"
        )
        self.assertEqual(config.instances[0].params["totalrecords"], 100)
        self.assertEqual(config.instances[0].params["records"], 10)

    def test_blank_nodes_only_controls_orchestrator_executable_selection(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n  nodes: '   '\n  fullcopy: false\n"
        )
        self.assertFalse(config.instances[0].uses_gem)
        self.assertIs(config.instances[0].params["fullcopy"], False)

    def test_sbk_numeric_values_are_not_validated_by_analytics(self):
        config = self._load(
            "benchmarks:\n  file-run:\n    file: {}\n"
            "sbk:\n"
            "  idletimeoutseconds: invalid\n"
            "  totalrecords: -1\n"
        )
        self.assertEqual(
            config.instances[0].params["idletimeoutseconds"], "invalid"
        )
        self.assertEqual(config.instances[0].params["totalrecords"], -1)

    def test_minio_options_are_left_for_sbk_to_validate(self):
        for removed_option in ("endpoint", "endpoints"):
            for include_url in (False, True):
                url = "      url: http://node-a:9020\n" if include_url else ""
                with self.subTest(option=removed_option, include_url=include_url):
                    config = self._load(
                        "benchmarks:\n"
                        "  minio-run:\n"
                        "    minio:\n"
                        f"{url}"
                        f"      {removed_option}: http://node-b:9020\n"
                    )
                    self.assertEqual(
                        config.instances[0].params[removed_option],
                        "http://node-b:9020",
                    )

    def test_minio_10_7_values_are_preserved(self):
        config = self._load(
            "benchmarks:\n"
            "  minio-run:\n"
            "    MinIO:\n"
            "      url: http://node-a:9020,http://node-b:9020\n"
            "      endpoint-preflight: all\n"
            "      endpoint-metrics: true\n"
            "      range-offset-distribution: sequential\n"
            "      retry-strategy: exponential\n"
            "      retry-jitter: true\n"
            "      warmup-operation: put-get\n"
            "      mixed-read-source: catalog\n"
            "      auth-version: 4\n"
        )
        params = config.instances[0].params
        self.assertEqual(params["endpoint-preflight"], "all")
        self.assertIs(params["endpoint-metrics"], True)

        future = self._load(
            "benchmarks:\n"
            "  minio-run:\n"
            "    minio:\n"
            "      endpoint-preflight: future-mode\n"
            "      future-minio-option: enabled\n"
        )
        self.assertEqual(
            future.instances[0].params["endpoint-preflight"], "future-mode"
        )
        self.assertEqual(
            future.instances[0].params["future-minio-option"], "enabled"
        )


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
