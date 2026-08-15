import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analytics.cli import _print_charts_resolution, _print_sbk_resolution
from analytics.releases import (
    ChartsInstall,
    DependencySource,
    SbkInstall,
    ensure_sbk,
    ensure_sbk_charts,
    resolve_local_sbk,
    resolve_local_sbk_charts,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _sbk_home(root: Path) -> Path:
    _executable(root / "bin" / "sbk-yal")
    _executable(root / "bin" / "sbk-gem-yal")
    return root


class LocalSbkResolutionTests(unittest.TestCase):
    def test_distribution_layout_is_used_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _sbk_home(Path(directory))
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            install = resolve_local_sbk(root)

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(install.source, DependencySource.LOCAL)
            self.assertEqual(install.home, root.resolve())
            self.assertEqual(install.sbk_yal, (root / "bin" / "sbk-yal").resolve())
            self.assertEqual(before, after)

    def test_built_source_checkout_layout_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = _sbk_home(root / "build" / "install" / "sbk")

            install = resolve_local_sbk(root)

            self.assertEqual(install.source, DependencySource.LOCAL)
            self.assertEqual(install.home, home.resolve())

    def test_gem_executable_is_only_required_for_gem_workloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _executable(root / "bin" / "sbk-yal")

            install = resolve_local_sbk(root)

            self.assertIsNone(install.sbk_gem_yal)
            with self.assertRaisesRegex(RuntimeError, "sbk-gem-yal"):
                resolve_local_sbk(root, require_gem=True)

    def test_invalid_explicit_folder_never_falls_back_to_github(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with mock.patch("analytics.releases._gh_release") as github:
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    ensure_sbk("10.4", local_folder=missing)
            github.assert_not_called()

    def test_managed_cache_is_identified(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            cache = downloads / "10.4"
            home = _sbk_home(cache / "extracted" / "sbk")
            cache.mkdir(parents=True, exist_ok=True)
            (cache / ".home").write_text(str(home), encoding="utf-8")
            (cache / ".ok").touch()

            with mock.patch("analytics.releases._gh_release") as github:
                install = ensure_sbk("10.4", downloads_folder=downloads)

            self.assertEqual(install.source, DependencySource.MANAGED_CACHE)
            github.assert_not_called()

    def test_new_release_is_identified_as_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)

            def fake_download(_url, archive, **_kwargs):
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.touch()
                return "a" * 64

            def fake_extract(_archive, destination):
                return _sbk_home(destination / "sbk")

            release = {
                "assets": [
                    {
                        "name": "sbk-10.4.tar",
                        "browser_download_url": "https://example/sbk-10.4.tar",
                        "digest": f"sha256:{'a' * 64}",
                    }
                ]
            }
            with mock.patch("analytics.releases._gh_release", return_value=release), \
                    mock.patch("analytics.releases._download", side_effect=fake_download), \
                    mock.patch("analytics.releases._extract", side_effect=fake_extract):
                install = ensure_sbk("10.4", downloads_folder=downloads)

            self.assertEqual(install.source, DependencySource.DOWNLOADED)
            self.assertTrue((downloads / "10.4" / ".ok").is_file())
            self.assertTrue((downloads / "10.4" / "metadata.json").is_file())
            self.assertFalse(list(downloads.glob(".10.4.install-*")))

    def test_release_digest_mismatch_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            release = {"assets": [{
                "name": "sbk-10.4.tar",
                "browser_download_url": "https://example/sbk-10.4.tar",
                "digest": f"sha256:{'a' * 64}",
            }]}

            def fake_download(_url, archive, **_kwargs):
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.touch()
                return "b" * 64

            with mock.patch(
                "analytics.releases._gh_release", return_value=release
            ), mock.patch(
                "analytics.releases._download", side_effect=fake_download
            ), mock.patch("analytics.releases._extract") as extract:
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    ensure_sbk("10.4", downloads_folder=downloads)
            extract.assert_not_called()


class LocalChartsResolutionTests(unittest.TestCase):
    def test_source_checkout_layout_is_used_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = _executable(root / "sbk-charts")
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            install = resolve_local_sbk_charts(root)

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(install.source, DependencySource.LOCAL)
            self.assertEqual(install.cli, cli.resolve())
            self.assertEqual(before, after)

    def test_environment_root_layout_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = _executable(root / "bin" / "sbk-charts")

            install = resolve_local_sbk_charts(root)

            self.assertEqual(install.cli, cli.resolve())

    def test_local_folder_overrides_conda(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = _executable(root / "sbk-charts")
            with mock.patch.dict(os.environ, {"CONDA_PREFIX": "/conda"}), \
                    mock.patch("analytics.releases.subprocess.run") as run:
                install = ensure_sbk_charts("4.26.7.1", local_folder=root)

            self.assertEqual(install.source, DependencySource.LOCAL)
            self.assertEqual(install.cli, cli.resolve())
            run.assert_called_once_with(
                [str(cli), "-h"], capture_output=True, text=True,
                timeout=60,
            )

    def test_invalid_explicit_folder_never_runs_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("analytics.releases.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "no supported executable"):
                    ensure_sbk_charts("4.26.7.1", local_folder=root)
            run.assert_not_called()

    def test_managed_cache_is_identified(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            cache = downloads / "sbk-charts" / "4.26.7.1"
            _executable(cache / "venv" / "bin" / "python")
            _executable(cache / "venv" / "bin" / "sbk-charts")
            (cache / ".ok").touch()

            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("analytics.releases.subprocess.run") as run:
                install = ensure_sbk_charts(
                    "4.26.7.1", downloads_folder=downloads
                )

            self.assertEqual(install.source, DependencySource.MANAGED_CACHE)
            run.assert_not_called()

    def test_new_install_is_identified_as_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)

            class FakeVenvBuilder:
                def __init__(self, **_kwargs):
                    pass

                def create(self, venv_dir):
                    _executable(Path(venv_dir) / "bin" / "python")
                    _executable(Path(venv_dir) / "bin" / "sbk-charts")

            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch(
                        "analytics.releases.venv.EnvBuilder",
                        FakeVenvBuilder,
                    ), mock.patch("analytics.releases.subprocess.run") as run:
                install = ensure_sbk_charts(
                    "4.26.7.1", downloads_folder=downloads
                )

            self.assertEqual(install.source, DependencySource.DOWNLOADED)
            self.assertEqual(run.call_count, 2)
            cache = downloads / "sbk-charts" / "4.26.7.1"
            self.assertTrue((cache / ".ok").is_file())
            self.assertTrue((cache / "metadata.json").is_file())
            self.assertFalse(list(cache.parent.glob(".4.26.7.1.install-*")))


class ResolutionOutputTests(unittest.TestCase):
    def test_local_sources_are_printed_with_exact_paths(self):
        sbk = SbkInstall(
            home=Path("/local/SBK"),
            source=DependencySource.LOCAL,
            _sbk_yal=Path("/local/SBK/bin/sbk-yal"),
            _sbk_gem_yal=Path("/local/SBK/bin/sbk-gem-yal"),
        )
        charts = ChartsInstall(
            venv_dir=Path("/local/sbk-charts"),
            source=DependencySource.LOCAL,
            _cli=Path("/local/sbk-charts/sbk-charts"),
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            _print_sbk_resolution(sbk, "10.4")
            _print_charts_resolution(charts, "4.26.7.1")

        text = output.getvalue()
        # Windows runners may use a legacy console encoding such as cp1252.
        # Dependency status output must remain printable there.
        text.encode("cp1252")
        self.assertIn("SBK source       : LOCAL", text)
        self.assertIn("sbk-charts source: LOCAL", text)
        self.assertIn("/local/SBK/bin/sbk-yal", text)
        self.assertIn("/local/sbk-charts/sbk-charts", text)
        self.assertIn("detected version : unknown", text)
        self.assertIn("configured version: 10.4 (policy applies)", text)
        self.assertIn("configured version: 4.26.7.1 (policy applies)", text)


if __name__ == "__main__":
    unittest.main()
