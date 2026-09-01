import tempfile
import unittest
from pathlib import Path

from analytics.properties import parse_properties


class PropertiesTests(unittest.TestCase):
    def _parse(self, *extra_lines: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            properties = root / "sbk-config.env"
            properties.write_text(
                "\n".join(
                    [
                        "sbk.version=10.6",
                        "sbk-charts.version=4.26.7.1",
                        *extra_lines,
                    ]
                )
            )
            versions = parse_properties(properties)
            return versions, root

    def test_downloads_folder_is_used(self):
        versions, root = self._parse("downloads.folder=./dependencies")

        self.assertEqual(versions.downloads_folder, root / "dependencies")

    def test_local_folders_are_resolved_relative_to_properties_file(self):
        versions, root = self._parse(
            "sbk.local.folder=../SBK",
            "sbk-charts.local.folder=./charts",
        )

        self.assertEqual(versions.sbk_local_folder, root / "../SBK")
        self.assertEqual(versions.sbk_charts_local_folder, root / "charts")

    def test_local_folders_are_optional(self):
        versions, _ = self._parse()

        self.assertIsNone(versions.sbk_local_folder)
        self.assertIsNone(versions.sbk_charts_local_folder)

    def test_charts_source_digest_is_validated(self):
        digest = "a" * 64
        versions, _ = self._parse(f"sbk-charts.sha256={digest.upper()}")
        self.assertEqual(versions.sbk_charts_sha256, digest)
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            self._parse("sbk-charts.sha256=not-a-digest")


if __name__ == "__main__":
    unittest.main()
