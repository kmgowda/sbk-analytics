import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from analytics.config import load_config
from analytics.runner import run_jobs
from analytics.yaml_gen import generate_instance_yaml


class BenchmarkKeyTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(content, encoding="utf-8")
            return load_config(config_path)

    def test_benchmarks_is_the_canonical_key(self):
        config = self._load("benchmarks: [file, rocksdb]\n")
        self.assertEqual(
            [instance.class_name for instance in config.instances],
            ["file", "rocksdb"],
        )

    def test_legacy_classes_key_is_accepted_with_warning(self):
        with self.assertLogs("analytics.config", level="WARNING") as captured:
            config = self._load("classes: [file]\n")

        self.assertEqual(config.instances[0].class_name, "file")
        self.assertIn("deprecated", "\n".join(captured.output))
        self.assertIn("benchmarks", "\n".join(captured.output))

    def test_benchmarks_and_legacy_classes_cannot_be_combined(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self._load("benchmarks: [file]\nclasses: [rocksdb]\n")

    def test_empty_benchmarks_is_rejected_using_canonical_name(self):
        with self.assertRaisesRegex(ValueError, "'benchmarks'"):
            self._load("benchmarks: []\n")


class ConfigBooleanTests(unittest.TestCase):
    def _load_chat(self, value: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text(
                f"benchmarks: [file]\nsbk-charts:\n  chat: {value}\n",
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


class ExecutionModeTests(unittest.TestCase):
    def test_empty_nodes_uses_local_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yml"
            config_path.write_text(
                "benchmarks: [file]\nsbk:\n  nodes: []\n",
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
                "  - class: file\n"
                "    name: local\n"
                "  - class: file\n"
                "    name: remote\n"
                "    nodes: [node1]\n",
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
            self.assertIn("sbkGemArgs", yaml.safe_load(remote_yml.read_text()))

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
