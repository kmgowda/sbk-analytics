import contextlib
import io
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from analytics.config import load_config
from analytics.cli import main
from analytics.policy import RUNTIME_POLICY
from analytics.runner import run_jobs
from analytics.yaml_gen import generate_instance_yaml


class BenchmarkKeyTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(content, encoding="utf-8")
            return load_config(config_path)

    def test_benchmarks_is_the_canonical_key(self):
        config = self._load(
            "benchmarks:\n"
            "  file-write:\n"
            "    file: {}\n"
            "  rocksdb-write:\n"
            "    rocksdb: {}\n"
        )
        self.assertEqual(
            [instance.class_name for instance in config.instances],
            ["file", "rocksdb"],
        )
        self.assertEqual(
            [instance.name for instance in config.instances],
            ["file-write", "rocksdb-write"],
        )

    def test_legacy_classes_key_is_accepted_with_warning(self):
        with self.assertLogs("analytics.config", level="WARNING") as captured:
            config = self._load("classes: [file]\n")

        self.assertEqual(config.instances[0].class_name, "file")
        self.assertIn("deprecated", "\n".join(captured.output))
        self.assertIn("benchmarks", "\n".join(captured.output))

    def test_legacy_benchmark_sequence_is_accepted_with_warning(self):
        with self.assertLogs("analytics.config", level="WARNING") as captured:
            config = self._load("benchmarks: [file]\n")

        self.assertEqual(config.instances[0].class_name, "file")
        self.assertIn("sequence format is deprecated", "\n".join(captured.output))

    def test_benchmarks_and_legacy_classes_cannot_be_combined(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self._load("benchmarks: [file]\nclasses: [rocksdb]\n")

    def test_empty_benchmarks_is_rejected_using_canonical_name(self):
        with self.assertRaisesRegex(ValueError, "'benchmarks'"):
            self._load("benchmarks: {}\n")

    def test_canonical_instance_requires_exactly_one_class_group(self):
        invalid_documents = (
            "benchmarks:\n  run: {}\n",
            "benchmarks:\n  run:\n    file: {}\n    rocksdb: {}\n",
        )
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaisesRegex(
                ValueError, "exactly one SBK class mapping"
            ):
                self._load(document)

    def test_canonical_class_parameters_must_be_a_mapping(self):
        with self.assertRaisesRegex(
            ValueError, "class parameters must be a mapping"
        ):
            self._load("benchmarks:\n  run:\n    file: invalid\n")

    def test_instance_parameters_override_shared_sbk_values(self):
        config = self._load(
            "sbk:\n"
            "  time: ms\n"
            "  size: 4096\n"
            "  writers: 1\n"
            "benchmarks:\n"
            "  large-write:\n"
            "    file:\n"
            "      size: 65536\n"
            "      file: /tmp/data\n"
        )
        instance = config.instances[0]
        self.assertEqual(instance.name, "large-write")
        self.assertEqual(instance.class_name, "file")
        self.assertEqual(instance.params["time"], "ms")
        self.assertEqual(instance.params["writers"], 1)
        self.assertEqual(instance.params["size"], 65536)

    def test_duplicate_instance_section_is_rejected_before_yaml_overwrite(self):
        with self.assertRaisesRegex(ValueError, "duplicate YAML key 'same-run'"):
            self._load(
                "benchmarks:\n"
                "  same-run:\n    file: {}\n"
                "  same-run:\n    rocksdb: {}\n"
            )

    def test_filename_normalized_instance_names_must_be_unique(self):
        with self.assertRaisesRegex(ValueError, "duplicate instance name"):
            self._load(
                "benchmarks:\n"
                "  run one:\n    file: {}\n"
                "  run_one:\n    rocksdb: {}\n"
            )

    def test_unknown_orchestrator_key_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "unknown sbk-analytics top-level key: 'cleaup_before_run'",
        ):
            self._load(
                "benchmarks:\n  run:\n    file: {}\n"
                "cleaup_before_run: true\n"
            )

    def test_multiple_unknown_orchestrator_keys_are_reported(self):
        with self.assertRaisesRegex(
            ValueError,
            "unknown sbk-analytics top-level keys",
        ) as captured:
            self._load(
                "benchmarks:\n  run:\n    file: {}\n"
                "workflow_name: smoke\n"
                "retry_failed_runs: true\n"
            )

        self.assertIn("'workflow_name'", str(captured.exception))
        self.assertIn("'retry_failed_runs'", str(captured.exception))

    def test_non_string_top_level_key_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "unknown sbk-analytics top-level key: 42",
        ):
            self._load("benchmarks:\n  run:\n    file: {}\n42: value\n")

    def test_cli_exits_with_handled_error_for_unknown_orchestrator_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(
                "benchmarks:\n  run:\n    file: {}\n"
                "cleaup_before_run: true\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return_code = main(["-c", str(config_path)])

        self.assertEqual(return_code, RUNTIME_POLICY.exit_codes.handled_error)
        self.assertIn("unknown sbk-analytics top-level key", stderr.getvalue())


class ConfigBooleanTests(unittest.TestCase):
    def _load_chat(self, value: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(
                "benchmarks:\n  run:\n    file: {}\n"
                f"sbk-charts:\n  chat: {value}\n",
                encoding="utf-8",
            )
            return load_config(config_path).chat

    def test_quoted_false_is_false(self):
        self.assertFalse(self._load_chat('"false"'))

    def test_supported_boolean_spellings(self):
        for value in ("true", '"yes"', '"on"', "1"):
            with self.subTest(value=value):
                self.assertTrue(self._load_chat(value))
        for value in ("false", '"no"', '"off"', "0"):
            with self.subTest(value=value):
                self.assertFalse(self._load_chat(value))

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "sbk-charts.chat"):
            self._load_chat('"sometimes"')


class DownstreamConfigurationBoundaryTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(content, encoding="utf-8")
            return load_config(config_path)

    def test_sbk_parameters_are_forwarded_without_contract_validation(self):
        config = self._load(
            "sbk:\n"
            "  future-sbk-option: enabled\n"
            "  runtimecleanup: legacy-value\n"
            "benchmarks:\n"
            "  future-run:\n"
            "    future-driver:\n"
            "      future-driver-option: 17\n"
        )

        params = config.instances[0].params
        self.assertEqual(params["future-sbk-option"], "enabled")
        self.assertEqual(params["runtimecleanup"], "legacy-value")
        self.assertEqual(params["future-driver-option"], 17)
        self.assertNotIn("packagescleanup", params)

    def test_sbk_charts_model_and_plugin_params_are_not_catalog_validated(self):
        config = self._load(
            "benchmarks:\n  run:\n    file: {}\n"
            "sbk-charts:\n"
            "  ai_model: future-backend\n"
            "  ai_params:\n"
            "    future-plugin-option: value\n"
        )

        self.assertEqual(config.ai_model, "future-backend")
        self.assertEqual(
            config.ai_params,
            {"future-plugin-option": "value"},
        )


class ExecutionModeTests(unittest.TestCase):
    def test_empty_nodes_uses_local_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yml"
            config_path.write_text(
                "benchmarks:\n  local:\n    file: {}\n"
                "sbk:\n  nodes: []\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            instance = config.instances[0]
            generated = generate_instance_yaml(
                instance, root / "yml", root / "result.csv"
            )

            self.assertFalse(config.uses_gem)
            self.assertFalse(instance.uses_gem)
            self.assertIn("sbkArgs", yaml.safe_load(generated.read_text()))

    def test_mixed_instances_use_matching_wrappers_and_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yml"
            config_path.write_text(
                "benchmarks:\n"
                "  local:\n"
                "    file: {}\n"
                "  remote:\n"
                "    file:\n"
                "      nodes: [node1]\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            local, remote = config.instances
            self.assertTrue(config.uses_gem)
            self.assertFalse(local.uses_gem)
            self.assertTrue(remote.uses_gem)

            local_yml = generate_instance_yaml(
                local, root / "yml", root / "local.csv"
            )
            remote_yml = generate_instance_yaml(
                remote, root / "yml", root / "remote.csv"
            )
            self.assertIn("sbkArgs", yaml.safe_load(local_yml.read_text()))
            remote_document = yaml.safe_load(remote_yml.read_text())
            self.assertIn("sbkGemArgs", remote_document)
            interface = RUNTIME_POLICY.sbk_interface
            self.assertEqual(
                remote_document[interface.gem_arguments_wrapper][
                    interface.output_option
                ],
                interface.gem_csv_logger,
            )

            for mode in ("serial", "parallel"):
                with self.subTest(mode=mode):
                    local_marker = root / f"local-ran-{mode}"
                    remote_marker = root / f"remote-ran-{mode}"
                    local_executable = self._fake_executable(
                        root / f"local-{mode}", local_marker
                    )
                    remote_executable = self._fake_executable(
                        root / f"remote-{mode}", remote_marker
                    )
                    results = run_jobs(
                        local_executable,
                        [
                            (local.name, local_yml, root / "local.csv"),
                            (remote.name, remote_yml, root / "remote.csv"),
                        ],
                        mode=mode,
                        log_dir=root / f"logs-{mode}",
                        executables={
                            local.name: local_executable,
                            remote.name: remote_executable,
                        },
                    )

                    self.assertEqual(
                        [result.returncode for result in results], [0, 0]
                    )
                    self.assertTrue(local_marker.exists())
                    self.assertTrue(remote_marker.exists())

    @staticmethod
    def _fake_executable(path: Path, marker: Path) -> Path:
        path.write_text(
            "#!/bin/sh\n" f"touch '{marker}'\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


if __name__ == "__main__":
    unittest.main()
