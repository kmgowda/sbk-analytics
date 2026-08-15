import tempfile
import unittest
from pathlib import Path

from analytics.properties import parse_properties


class PropertiesTests(unittest.TestCase):
    def _parse(self, folder_line: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            properties = root / "sbk-config.env"
            properties.write_text(
                "\n".join(
                    [
                        "sbk.version=10.4",
                        "sbk-charts.version=4.26.7.1",
                        folder_line,
                    ]
                )
            )
            versions = parse_properties(properties)
            return versions, root

    def test_downloads_folder_is_used(self):
        versions, root = self._parse("downloads.folder=./dependencies")

        self.assertEqual(versions.downloads_folder, root / "dependencies")


if __name__ == "__main__":
    unittest.main()
