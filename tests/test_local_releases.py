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
    SourceProvenance,
    ensure_sbk,
    ensure_sbk_charts,
    _gh_release,
    _git_details,
    inspect_shared_sbk,
    inspect_shared_sbk_charts,
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
    def test_release_lookup_accepts_v_prefixed_github_tag(self):
        missing = mock.Mock(status_code=404)
        found = mock.Mock(status_code=200)
        found.json.return_value = {"tag_name": "v11.0", "assets": []}
        with mock.patch(
            "analytics.releases.requests.get", side_effect=(missing, found)
        ) as request:
            release = _gh_release("owner/repository", "11.0", ssl_verify=True)

        self.assertEqual(release["tag_name"], "v11.0")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args_list[1].args[0].endswith("/v11.0"))

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
            self.assertEqual(install.provenance.mode, "shared-folder")
            self.assertEqual(install.provenance.layout, "gradle-install")
            self.assertEqual(install.provenance.configured_location, str(root.resolve()))
            self.assertEqual(install.provenance.resolved_location, str(home.resolve()))

    def test_shared_status_is_read_only_and_never_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = _sbk_home(root / "build" / "install" / "sbk")
            with mock.patch("analytics.releases.subprocess.run") as run:
                status = inspect_shared_sbk(root)

            run.assert_not_called()
            self.assertTrue(status["valid"])
            self.assertTrue(status["read_only"])
            self.assertFalse(status["build_performed"])
            self.assertEqual(status["layout"], "gradle-install")
            self.assertEqual(status["resolved_location"], str(home.resolve()))

    def test_shared_checkout_reports_git_revision_and_dirty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            _sbk_home(root / "build" / "install" / "sbk")
            revision = mock.Mock(returncode=0, stdout="abc123def456\n")
            changes = mock.Mock(returncode=0, stdout=" M sbk-yal/source.java\n")
            with mock.patch(
                "analytics.releases._command_version", return_value="10.7"
            ), mock.patch(
                "analytics.releases.subprocess.run",
                side_effect=(revision, changes),
            ) as run:
                install = resolve_local_sbk(root)

            self.assertEqual(run.call_count, 2)
            status_command = run.call_args_list[1].args[0]
            self.assertIn("--untracked-files=no", status_command)
            self.assertNotIn("--untracked-files=normal", status_command)
            self.assertEqual(install.provenance.revision, "abc123def456")
            self.assertTrue(install.provenance.dirty)

    def test_git_inspection_failure_is_visible_at_debug_level(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with mock.patch(
                "analytics.releases.subprocess.run",
                side_effect=OSError("git unavailable"),
            ), self.assertLogs("analytics.releases", level="DEBUG") as logs:
                revision, dirty = _git_details(root)

            self.assertIsNone(revision)
            self.assertIsNone(dirty)
            self.assertTrue(
                any("Git provenance command failed" in line for line in logs.output)
            )

    def test_status_and_resolution_share_sbk_layout_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "bin" / "sbk-yal"
            blocked.parent.mkdir(parents=True)
            blocked.write_text("#!/bin/sh\n", encoding="utf-8")
            _sbk_home(root / "build" / "install" / "sbk")

            status = inspect_shared_sbk(root)

            self.assertEqual(status["layout"], "distribution")
            self.assertFalse(status["valid"])
            with self.assertRaisesRegex(RuntimeError, "not executable"):
                resolve_local_sbk(root)

    def test_shared_status_explains_that_missing_build_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            status = inspect_shared_sbk(Path(directory))

            self.assertFalse(status["valid"])
            self.assertIn("does not build shared SBK folders", status["error"])

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
                    ensure_sbk("10.6", local_folder=missing)
            github.assert_not_called()

    def test_managed_cache_is_identified(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            cache = downloads / "10.6"
            home = _sbk_home(cache / "extracted" / "sbk")
            cache.mkdir(parents=True, exist_ok=True)
            (cache / ".home").write_text(str(home), encoding="utf-8")
            (cache / ".ok").touch()

            with mock.patch("analytics.releases._gh_release") as github:
                install = ensure_sbk("10.6", downloads_folder=downloads)

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
                        "name": "sbk-10.6.tar",
                        "browser_download_url": "https://example/sbk-10.6.tar",
                        "digest": f"sha256:{'a' * 64}",
                    }
                ]
            }
            with mock.patch("analytics.releases._gh_release", return_value=release), \
                    mock.patch("analytics.releases._download", side_effect=fake_download), \
                    mock.patch("analytics.releases._extract", side_effect=fake_extract):
                install = ensure_sbk("10.6", downloads_folder=downloads)

            self.assertEqual(install.source, DependencySource.DOWNLOADED)
            self.assertTrue((downloads / "10.6" / ".ok").is_file())
            self.assertTrue((downloads / "10.6" / "metadata.json").is_file())
            self.assertFalse(list(downloads.glob(".10.6.install-*")))

    def test_release_digest_mismatch_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            release = {"assets": [{
                "name": "sbk-10.6.tar",
                "browser_download_url": "https://example/sbk-10.6.tar",
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
                    ensure_sbk("10.6", downloads_folder=downloads)
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
            self.assertEqual(install.provenance.mode, "shared-folder")
            self.assertEqual(install.provenance.layout, "source-launcher")
            self.assertEqual(before, after)

    def test_shared_charts_status_does_not_start_or_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = _executable(root / "sbk-charts")
            with mock.patch("analytics.releases.subprocess.run") as run:
                status = inspect_shared_sbk_charts(root)

            run.assert_not_called()
            self.assertTrue(status["valid"])
            self.assertTrue(status["read_only"])
            self.assertFalse(status["install_performed"])
            self.assertEqual(status["layout"], "source-launcher")
            self.assertEqual(status["executable"], str(cli.resolve()))

    def test_environment_root_layout_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = _executable(root / "bin" / "sbk-charts")

            install = resolve_local_sbk_charts(root)

            self.assertEqual(install.cli, cli.resolve())

    def test_status_and_resolution_share_charts_layout_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "sbk-charts"
            blocked.write_text("#!/bin/sh\n", encoding="utf-8")
            _executable(root / "bin" / "sbk-charts")

            status = inspect_shared_sbk_charts(root)

            self.assertEqual(status["layout"], "source-launcher")
            self.assertFalse(status["valid"])
            with self.assertRaisesRegex(RuntimeError, "not executable"):
                resolve_local_sbk_charts(root)

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
                [str(cli.resolve()), "-h"], capture_output=True, text=True,
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
            self.assertEqual(install.provenance.mode, "github-release")
            self.assertEqual(install.provenance.release_tag, "4.26.7.1")
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

    def test_verified_source_archive_avoids_git(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            digest = "a" * 64

            class FakeVenvBuilder:
                def __init__(self, **_kwargs):
                    pass

                def create(self, venv_dir):
                    _executable(Path(venv_dir) / "bin" / "python")
                    _executable(Path(venv_dir) / "bin" / "sbk-charts")

            def fake_download(url, archive, **_kwargs):
                self.assertIn("/archive/refs/tags/4.26.7.1.tar.gz", url)
                archive.write_bytes(b"verified source")
                return digest

            with mock.patch(
                "analytics.releases.venv.EnvBuilder", FakeVenvBuilder
            ), mock.patch(
                "analytics.releases._download", side_effect=fake_download
            ), mock.patch("analytics.releases.subprocess.run") as run:
                install = ensure_sbk_charts(
                    "4.26.7.1",
                    downloads_folder=downloads,
                    source_sha256=digest,
                )

            self.assertEqual(install.source, DependencySource.DOWNLOADED)
            install_command = run.call_args_list[-1].args[0]
            self.assertFalse(any(str(value).startswith("git+") for value in install_command))
            metadata = (downloads / "sbk-charts" / "4.26.7.1" / "metadata.json")
            self.assertIn(digest, metadata.read_text())

    def test_source_archive_digest_mismatch_is_rejected_before_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)

            class FakeVenvBuilder:
                def __init__(self, **_kwargs):
                    pass

                def create(self, venv_dir):
                    _executable(Path(venv_dir) / "bin" / "python")

            with mock.patch(
                "analytics.releases.venv.EnvBuilder", FakeVenvBuilder
            ), mock.patch(
                "analytics.releases._download", return_value="b" * 64
            ), mock.patch("analytics.releases.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    ensure_sbk_charts(
                        "4.26.7.1",
                        downloads_folder=downloads,
                        source_sha256="a" * 64,
                    )
            run.assert_not_called()


class ResolutionOutputTests(unittest.TestCase):
    def test_local_sources_are_printed_with_exact_paths(self):
        sbk = SbkInstall(
            home=Path("/local/SBK"),
            source=DependencySource.LOCAL,
            _sbk_yal=Path("/local/SBK/bin/sbk-yal"),
            _sbk_gem_yal=Path("/local/SBK/bin/sbk-gem-yal"),
            provenance=SourceProvenance(
                mode="shared-folder",
                layout="gradle-install",
                configured_location="/shared/SBK",
                resolved_location="/local/SBK",
                revision="abc123",
                dirty=True,
            ),
        )
        charts = ChartsInstall(
            venv_dir=Path("/local/sbk-charts"),
            source=DependencySource.LOCAL,
            _cli=Path("/local/sbk-charts/sbk-charts"),
            provenance=SourceProvenance(
                mode="shared-folder",
                layout="source-launcher",
                configured_location="/shared/sbk-charts",
                resolved_location="/local/sbk-charts/sbk-charts",
                revision="def456",
                dirty=False,
            ),
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            _print_sbk_resolution(sbk, "10.6")
            _print_charts_resolution(charts, "4.26.7.1")

        text = output.getvalue()
        # Dependency status output remains printable in legacy encodings.
        text.encode("cp1252")
        self.assertIn("SBK source       : LOCAL", text)
        self.assertIn("sbk-charts source: LOCAL", text)
        self.assertIn("/local/SBK/bin/sbk-yal", text)
        self.assertIn("/local/sbk-charts/sbk-charts", text)
        self.assertIn("shared folder (read-only)", text)
        self.assertIn("layout           : gradle-install", text)
        self.assertIn("configured path  : /shared/SBK", text)
        self.assertIn("Git revision     : abc123 (dirty)", text)
        self.assertIn(
            "local action     : validate and execute only; SBK build is external",
            text,
        )
        self.assertIn(
            "local action     : execute selected command; launcher owns its runtime",
            text,
        )
        self.assertIn("detected version : unknown", text)
        self.assertIn("configured version: 10.6 (policy applies)", text)
        self.assertIn("configured version: 4.26.7.1 (policy applies)", text)

    def test_release_sources_print_repository_artifact_and_digest(self):
        provenance = SourceProvenance(
            mode="github-release",
            layout="managed-install",
            resolved_location="/cache/sbk/10.6",
            repository_url="https://github.com/owner/SBK",
            release_tag="10.6",
            asset="sbk-10.6.tar",
            sha256="a" * 64,
        )
        install = SbkInstall(
            home=Path("/cache/sbk/10.6"),
            source=DependencySource.MANAGED_CACHE,
            provenance=provenance,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            _print_sbk_resolution(install, "10.6")

        text = output.getvalue()
        self.assertIn("selection        : GitHub release", text)
        self.assertIn("repository       : https://github.com/owner/SBK", text)
        self.assertIn("release tag      : 10.6", text)
        self.assertIn("release asset    : sbk-10.6.tar", text)
        self.assertIn(f"SHA-256          : {'a' * 64}", text)


if __name__ == "__main__":
    unittest.main()
