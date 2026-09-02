import tempfile
import unittest
from pathlib import Path

import yaml

from analytics.config import load_config
from analytics.policy import RUNTIME_POLICY
from analytics.yaml_gen import generate_instance_yaml


ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_EXAMPLES = ROOT / "examples" / "benchmarks"


class ShippedExampleTests(unittest.TestCase):
    def test_all_workflow_examples_use_canonical_benchmarks_key(self):
        workflows = sorted((ROOT / "examples").rglob("*.yml"))
        self.assertTrue(workflows)
        configuration = RUNTIME_POLICY.configuration
        for workflow in workflows:
            with self.subTest(workflow=workflow.relative_to(ROOT)):
                document = yaml.safe_load(workflow.read_text())
                self.assertIn(configuration.benchmarks_keys[0], document)
                self.assertNotIn(configuration.legacy_classes_keys[0], document)
                self.assertTrue(load_config(workflow).instances)


class StorageClassExampleTests(unittest.TestCase):
    def test_file_and_rocksdb_write_read_workflows_are_complete(self):
        expected = {
            BENCHMARK_EXAMPLES / driver / f"{operation}.yml"
            for driver in ("file", "rocksdb")
            for operation in ("write", "read")
        }
        local_examples = {
            path for path in BENCHMARK_EXAMPLES.glob("*/*.yml")
            if path.parent.name in {"file", "rocksdb"}
        }
        self.assertEqual(local_examples, expected)

        for driver in ("file", "rocksdb"):
            write = load_config(BENCHMARK_EXAMPLES / driver / "write.yml")
            read = load_config(BENCHMARK_EXAMPLES / driver / "read.yml")
            self.assertTrue(write.cleanup_before_run)
            self.assertFalse(read.cleanup_before_run)
            self.assertEqual(write.mode, "serial")
            self.assertEqual(read.mode, "serial")
            self.assertEqual(len(write.instances), 2)
            self.assertEqual(len(read.instances), 2)

            path_option = "file" if driver == "file" else "rfile"
            writes = {instance.params["size"]: instance for instance in write.instances}
            reads = {instance.params["size"]: instance for instance in read.instances}
            self.assertEqual(set(writes), {4096, 65536})
            self.assertEqual(set(reads), set(writes))
            for size, write_instance in writes.items():
                read_instance = reads[size]
                self.assertEqual(write_instance.class_name, driver)
                self.assertEqual(read_instance.class_name, driver)
                self.assertEqual(
                    write_instance.params[path_option],
                    read_instance.params[path_option],
                )
                self.assertEqual(
                    write_instance.params["records"],
                    read_instance.params["records"],
                )
                self.assertEqual(write_instance.params["writers"], 1)
                self.assertEqual(read_instance.params["readers"], 1)
                self.assertEqual(
                    Path(write_instance.params[path_option]).parent,
                    Path(write.workdir),
                )

    def test_class_workflows_render_valid_sbk_yal_documents(self):
        interface = RUNTIME_POLICY.sbk_interface
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for workflow in sorted(BENCHMARK_EXAMPLES.glob("*/*.yml")):
                config = load_config(workflow)
                for instance in config.instances:
                    rendered = generate_instance_yaml(
                        instance,
                        output / workflow.parent.name / workflow.stem,
                        output / f"{instance.name}.csv",
                    )
                    document = yaml.safe_load(rendered.read_text())
                    wrapper = (
                        interface.gem_arguments_wrapper
                        if instance.uses_gem
                        else interface.local_arguments_wrapper
                    )
                    self.assertEqual(list(document), [wrapper])
                    params = document[wrapper]
                    self.assertEqual(params[interface.class_option], instance.class_name)
                    expected_logger = (
                        interface.gem_csv_logger
                        if instance.uses_gem
                        else interface.csv_logger
                    )
                    self.assertEqual(params[interface.output_option], expected_logger)
                    self.assertEqual(
                        params[interface.csv_file_option],
                        str(output / f"{instance.name}.csv"),
                    )

    def test_minio_ecs_workflows_are_credential_free_and_complete(self):
        minio = BENCHMARK_EXAMPLES / "minio"
        expected = {
            minio / "ecs-obs-qualification.yml",
            minio / "ecs-obs-throughput.yml",
            minio / "ecs-obs-gem.yml",
        }
        self.assertEqual(set(minio.glob("*.yml")), expected)

        forbidden = {"key", "secret", "gempass"}
        for workflow in sorted(expected):
            with self.subTest(workflow=workflow.name):
                document = yaml.safe_load(workflow.read_text())
                config = load_config(workflow)
                self.assertEqual(config.mode, "serial")
                self.assertTrue(config.cleanup_before_run)
                self.assertTrue(config.instances)
                for instance in config.instances:
                    self.assertEqual(instance.class_name, "minio")
                    self.assertTrue(forbidden.isdisjoint(instance.params))
                    self.assertEqual(instance.params["bucket"], "sbk-analytics-ecs-obs")
                    self.assertEqual(instance.params["retry-max-attempts"], 1)
                    self.assertNotIn("endpoint-metrics", instance.params)
                self.assertNotIn("ChangeMe", workflow.read_text())

        qualification = load_config(minio / "ecs-obs-qualification.yml")
        operations = {instance.name for instance in qualification.instances}
        self.assertEqual(
            operations,
            {
                "ecs-put-1m", "ecs-get-1m", "ecs-range-get-4k",
                "ecs-list", "ecs-multipart-put-15m",
            },
        )
        gem = load_config(minio / "ecs-obs-gem.yml")
        self.assertTrue(all(instance.uses_gem for instance in gem.instances))


if __name__ == "__main__":
    unittest.main()
