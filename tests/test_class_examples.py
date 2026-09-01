import tempfile
import unittest
from pathlib import Path

import yaml

from analytics.config import load_config
from analytics.policy import RUNTIME_POLICY
from analytics.yaml_gen import generate_instance_yaml


ROOT = Path(__file__).resolve().parent.parent
CLASS_EXAMPLES = ROOT / "examples" / "classes"


class StorageClassExampleTests(unittest.TestCase):
    def test_file_and_rocksdb_write_read_workflows_are_complete(self):
        expected = {
            CLASS_EXAMPLES / driver / f"{operation}.yml"
            for driver in ("file", "rocksdb")
            for operation in ("write", "read")
        }
        self.assertEqual(set(CLASS_EXAMPLES.glob("*/*.yml")), expected)

        for driver in ("file", "rocksdb"):
            write = load_config(CLASS_EXAMPLES / driver / "write.yml")
            read = load_config(CLASS_EXAMPLES / driver / "read.yml")
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
            for workflow in sorted(CLASS_EXAMPLES.glob("*/*.yml")):
                config = load_config(workflow)
                for instance in config.instances:
                    rendered = generate_instance_yaml(
                        instance,
                        output / workflow.parent.name / workflow.stem,
                        output / f"{instance.name}.csv",
                    )
                    document = yaml.safe_load(rendered.read_text())
                    self.assertEqual(list(document), [interface.local_arguments_wrapper])
                    params = document[interface.local_arguments_wrapper]
                    self.assertEqual(params[interface.class_option], instance.class_name)
                    self.assertEqual(params[interface.output_option], interface.csv_logger)
                    self.assertEqual(
                        params[interface.csv_file_option],
                        str(output / f"{instance.name}.csv"),
                    )


if __name__ == "__main__":
    unittest.main()
