import contextlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from analytics.cli import _apply_overrides, _cleanup_benchmark_data, _parse_args, main
from analytics.config import Instance, OrchestratorConfig, load_config
from analytics.errors import CacheError, LocalPackageError
from analytics.properties import parse_properties
from analytics.releases import (
    ChartsInstall, DependencySource, JdkInstall, SbkInstall, _cache_lock,
    _charts_version, _extract, cache_root,
    resolve_local_sbk_charts,
)
from analytics.runner import RunResult


class PropertiesHardeningTests(unittest.TestCase):
    def _properties(self, root: Path, *extra: str):
        path = root / "sbk-config.env"
        path.write_text("\n".join((
            "sbk.version=10.4", "sbk-charts.version=4.26.7.1", *extra,
        )))
        return parse_properties(path)

    def test_tls_defaults_to_false_and_rejects_typo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(self._properties(root).ssl_verify)
            with self.assertRaisesRegex(ValueError, "ssl.verify"):
                self._properties(root, "ssl.verify=flase")

    def test_download_cache_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"SBK_ANALYTICS_CACHE": str(root / "env")}):
                versions = self._properties(root)
                self.assertIsNone(versions.downloads_folder)
                self.assertEqual(cache_root(), root / "env")
                args = _parse_args(["deps", "status", "--downloads-folder", str(root / "cli")])
                self.assertEqual(_apply_overrides(versions, args).downloads_folder, root / "cli")

    def test_direct_charts_executable_and_exact_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "charts with spaces"
            executable.write_text("#!/bin/sh\necho 'sbk-charts 1.2.3'\n")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            with mock.patch(
                "analytics.releases._charts_version", return_value="1.2.3"
            ):
                install = resolve_local_sbk_charts(
                    executable=executable, expected_version="1.2.3",
                    version_policy="exact",
                )
            self.assertEqual(install.cli, executable.resolve())
            with mock.patch(
                "analytics.releases._charts_version", return_value="1.2.3"
            ), self.assertRaisesRegex(LocalPackageError, "version mismatch"):
                resolve_local_sbk_charts(
                    executable=executable, expected_version="9.9.9",
                    version_policy="exact",
                )

    def test_charts_version_ignores_unrelated_dotted_versions(self):
        result = mock.Mock(returncode=0, stdout="Python 3.12.1\n", stderr="")
        with mock.patch("analytics.releases.subprocess.run", return_value=result), \
                mock.patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(_charts_version(Path("/tools/sbk-charts")))

    def test_charts_version_requires_product_prefix(self):
        result = mock.Mock(
            returncode=0, stdout="Sbk Charts Version : 4.26.7.1\n", stderr=""
        )
        with mock.patch("analytics.releases.subprocess.run", return_value=result):
            self.assertEqual(
                _charts_version(Path("/tools/sbk-charts")), "4.26.7.1"
            )

    def test_cache_lock_can_be_acquired(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "dependency.lock"
            with _cache_lock(lock):
                self.assertTrue(lock.is_file())


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escaped", "bad")
            with self.assertRaisesRegex(CacheError, "unsafe archive"):
                _extract(archive, root / "out")
            self.assertFalse((root / "escaped").exists())

    def test_tar_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar"
            info = tarfile.TarInfo("unsafe-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            with tarfile.open(archive, "w") as output:
                output.addfile(info)
            with self.assertRaisesRegex(CacheError, "link rejected"):
                _extract(archive, root / "out")


class CleanupSafetyTests(unittest.TestCase):
    def test_cleanup_only_removes_file_data_inside_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            inside = work / "data.bin"
            outside = root / "outside.bin"
            inside.write_text("x")
            outside.write_text("x")
            cfg = OrchestratorConfig(instances=[
                Instance("inside", "file", {"file": str(inside)}),
                Instance("outside", "file", {"file": str(outside)}),
            ], cleanup="on-success")
            removed = _cleanup_benchmark_data(cfg, work)
            self.assertEqual(removed, [inside.resolve()])
            self.assertFalse(inside.exists())
            self.assertTrue(outside.exists())

    def test_cleanup_value_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.yml"
            path.write_text("classes: [file]\ncleanup: everything\n")
            with self.assertRaisesRegex(ValueError, "cleanup"):
                load_config(path)


class CliFlowTests(unittest.TestCase):
    def test_charts_resolution_is_skipped_when_sbk_produces_no_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            properties = root / "sbk-config.env"
            properties.write_text(
                "sbk.version=10.4\nsbk-charts.version=4.26.7.1\n"
                "sbk.jdk.folder=./jdk\n"
            )
            config = root / "input.yml"
            config.write_text(
                f"workdir: {root / 'work'}\nclasses: [file]\n"
            )
            sbk_home = root / "sbk"
            sbk = SbkInstall(sbk_home, DependencySource.LOCAL,
                             _sbk_yal=sbk_home / "sbk-yal")
            failed = RunResult(
                "file", root / "job.yml", root / "missing.csv", None, 1, 0.1
            )
            with mock.patch("analytics.cli._print_banner"), \
                    mock.patch("analytics.cli.ensure_sbk", return_value=sbk), \
                    mock.patch("analytics.cli.ensure_jdk", return_value=JdkInstall(root)), \
                    mock.patch("analytics.cli.run_jobs", return_value=[failed]), \
                    mock.patch("analytics.cli.ensure_sbk_charts") as charts:
                rc = main(["-p", str(properties), "-c", str(config)])
            self.assertEqual(rc, 2)
            charts.assert_not_called()

    def test_json_status_is_one_parseable_document(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = main(["deps", "status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("sbk", payload)
        self.assertNotIn("Dependency status", stdout.getvalue())

    def test_json_dependency_error_is_one_parseable_document(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = main([
                "deps", "doctor", "--json", "--sbk-local",
                "/definitely/missing/sbk",
            ])
        self.assertEqual(rc, 5)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["exit_code"], 5)

    def test_json_config_init_is_one_parseable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "local.env"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = main([
                    "config", "init", "--output", str(output), "--json"
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["command"], "config init")
            self.assertTrue(output.is_file())

    def test_json_failed_benchmark_is_one_parseable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            properties = root / "sbk-config.env"
            properties.write_text(
                "sbk.version=10.4\nsbk-charts.version=4.26.7.1\n"
                "sbk.jdk.folder=./jdk\n"
            )
            config = root / "input.yml"
            config.write_text(
                f"workdir: {root / 'work'}\nclasses: [file]\n"
            )
            sbk = SbkInstall(
                root / "sbk", DependencySource.LOCAL,
                _sbk_yal=root / "sbk" / "sbk-yal",
            )
            failed = RunResult(
                "file", root / "job.yml", root / "missing.csv", None, 1, 0.1
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr), \
                    mock.patch("analytics.cli._print_banner"), \
                    mock.patch("analytics.cli.ensure_sbk", return_value=sbk), \
                    mock.patch(
                        "analytics.cli.ensure_jdk", return_value=JdkInstall(root)
                    ), mock.patch(
                        "analytics.cli.run_jobs", return_value=[failed]
                    ), mock.patch("analytics.cli.ensure_sbk_charts") as charts:
                rc = main([
                    "-p", str(properties), "-c", str(config), "--json"
                ])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(stdout.getvalue())["exit_code"], 2)
            charts.assert_not_called()

    def test_json_successful_benchmark_is_one_parseable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            properties = root / "sbk-config.env"
            properties.write_text(
                "sbk.version=10.4\nsbk-charts.version=4.26.7.1\n"
                "sbk.jdk.folder=./jdk\n"
            )
            config = root / "input.yml"
            config.write_text(
                f"workdir: {root / 'work'}\nclasses: [file]\n"
                "sbk-charts:\n  output: result.xlsx\n"
            )
            sbk = SbkInstall(
                root / "sbk", DependencySource.LOCAL,
                _sbk_yal=root / "sbk" / "sbk-yal",
            )
            charts = ChartsInstall(
                root / "charts", DependencySource.LOCAL,
                _cli=root / "charts" / "sbk-charts",
            )

            def fake_jobs(_executable, jobs, **_kwargs):
                name, yml, csv = jobs[0]
                csv.write_text("header\nvalue\n")
                return [RunResult(name, yml, csv, None, 0, 0.1)]

            def fake_charts(_install, _cfg, _csvs, output, **_kwargs):
                output.write_text("workbook")
                return 0

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr), \
                    mock.patch("analytics.cli._print_banner"), \
                    mock.patch("analytics.cli.ensure_sbk", return_value=sbk), \
                    mock.patch(
                        "analytics.cli.ensure_jdk", return_value=JdkInstall(root)
                    ), mock.patch(
                        "analytics.cli.ensure_sbk_charts", return_value=charts
                    ), mock.patch(
                        "analytics.cli.run_jobs", side_effect=fake_jobs
                    ), mock.patch(
                        "analytics.cli.run_sbk_charts", side_effect=fake_charts
                    ), mock.patch("analytics.cli.append_system_sheet"):
                rc = main([
                    "-p", str(properties), "-c", str(config), "--json"
                ])
            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(
                Path(payload["output"]),
                (root / "work" / "result.xlsx").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
