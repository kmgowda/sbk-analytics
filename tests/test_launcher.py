import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        for name in (
            "sbk-analytics",
            "sbk-analytics.sh",
            "sbk-bootstrap.env",
            "sbk-config.env",
            "pyproject.toml",
            "requirements.txt",
            "environment.yml",
            "MANIFEST.in",
            "uv.lock",
            ".python-version",
        ):
            shutil.copy2(ROOT / name, self.source / name)
        shutil.copytree(ROOT / "analytics", self.source / "analytics")
        shutil.copytree(ROOT / "examples", self.source / "examples")

        self.application = self.source / "sbk-analytics"
        self.launcher = self.source / "sbk-analytics.sh"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.log = self.root / "calls.log"
        self.fake_python = self.bin_dir / "managed-python"
        self.fake_python.write_text(
            """#!/usr/bin/env bash
set -u
printf 'python' >>"$FAKE_LOG"
printf '\t%s' "$@" >>"$FAKE_LOG"
printf '\n' >>"$FAKE_LOG"
if [[ "${1:-}" == "-P" ]]; then shift; fi
if [[ "${1:-}" == "-c" ]]; then exit 0; fi
if [[ "${1:-}" == "-m" && "${2:-}" == "analytics" ]]; then
    shift 2
    printf 'ARG=%s\n' "$@"
    exit "${FAKE_APP_EXIT:-0}"
fi
exit 1
""",
            encoding="utf-8",
        )
        self.fake_python.chmod(0o755)
        self.fake_uv = self.bin_dir / "uv"
        self.fake_uv.write_text(
            """#!/usr/bin/env bash
set -u
printf 'uv' >>"$FAKE_LOG"
printf '\t%s' "$@" >>"$FAKE_LOG"
printf '\n' >>"$FAKE_LOG"
printf 'uv-insecure-host\t%s\n' "${UV_INSECURE_HOST-}" >>"$FAKE_LOG"
if [[ "${1:-}" == "--version" ]]; then echo 'uv 0.12.5'; exit 0; fi
if [[ "${1:-}" == "python" && "${2:-}" == "install" ]]; then exit 0; fi
if [[ "${1:-}" == "venv" ]]; then
    stage=''
    for argument in "$@"; do
        [[ "$argument" == *'.install-'* ]] && stage="$argument"
    done
    [[ -n "$stage" ]] || exit 2
    mkdir -p "$stage/bin"
    cp "$FAKE_PYTHON_TEMPLATE" "$stage/bin/python"
    chmod +x "$stage/bin/python"
    exit 0
fi
if [[ "${1:-}" == "sync" ]]; then
    sleep "${FAKE_UV_SYNC_DELAY:-0}"
    exit "${FAKE_UV_SYNC_FAIL:-0}"
fi
exit 2
""",
            encoding="utf-8",
        )
        self.fake_uv.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "FAKE_LOG": str(self.log),
            "FAKE_PYTHON_TEMPLATE": str(self.fake_python),
            "SBK_ANALYTICS_ENV_HOME": str(self.root / "managed state"),
            "SBK_ANALYTICS_UV_EXECUTABLE": str(self.fake_uv),
        })
        self.env.pop("VIRTUAL_ENV", None)
        self.env.pop("CONDA_PREFIX", None)
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash is required for launcher tests")

    def _run(self, *arguments, **env_updates):
        env = self.env.copy()
        env.update(env_updates)
        return subprocess.run(
            [self.bash, str(self.launcher), *arguments],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _run_application(self, *arguments, **env_updates):
        env = self.env.copy()
        env.update(env_updates)
        return subprocess.run(
            [str(self.application), *arguments],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _app_environments(self):
        app_root = self.root / "managed state" / "app"
        return [path for path in app_root.iterdir() if path.is_dir()]

    def test_no_system_python_bootstraps_and_preserves_arguments(self):
        result = self._run("--json", "value with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["ARG=--json", "ARG=value with spaces"],
        )
        self.assertIn("preparing isolated Python", result.stderr)
        self.assertIn("using managed application environment", result.stderr)
        calls = self.log.read_text()
        self.assertIn("uv\tpython\tinstall", calls)
        self.assertIn("uv\tvenv\t--managed-python", calls)
        self.assertIn("uv\tsync\t--active\t--locked\t--no-editable", calls)
        self.assertIn("--reinstall-package\tsbk-analytics", calls)
        self.assertIn(
            "uv-insecure-host\tgithub.com release-assets.githubusercontent.com",
            calls,
        )

    def test_bootstrap_tls_verification_defaults_to_false(self):
        policy = (self.source / "sbk-bootstrap.env").read_text()
        self.assertIn("SBK_ANALYTICS_BOOTSTRAP_TLS_VERIFY=false", policy)
        result = self._run("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        insecure_lines = [
            line for line in self.log.read_text().splitlines()
            if line.startswith("uv-insecure-host\t")
        ]
        self.assertTrue(insecure_lines)
        self.assertTrue(all(line != "uv-insecure-host\t" for line in insecure_lines))

    def test_no_arguments_are_supported(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python\t-P\t-m\tanalytics\n", self.log.read_text())

    def test_second_run_is_offline_and_does_not_invoke_uv(self):
        first = self._run("--version")
        self.assertEqual(first.returncode, 0, first.stderr)
        calls_after_first = self.log.read_text()
        uv_calls_after_first = calls_after_first.count("uv\t")
        self.fake_uv.unlink()
        second = self._run("--help", SBK_ANALYTICS_BOOTSTRAP_OFFLINE="1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text().count("uv\t"), uv_calls_after_first)
        self.assertIn("python\t-P\t-m\tanalytics\t--help", self.log.read_text())

    def test_active_environment_is_never_modified(self):
        active = self.root / "caller-venv"
        (active / "bin").mkdir(parents=True)
        shutil.copy2(self.fake_python, active / "bin" / "python")
        result = self._run("--version", VIRTUAL_ENV=str(active))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using managed application environment", result.stderr)
        self.assertFalse((active / ".sbk-analytics-bootstrap").exists())

    def test_unified_application_preserves_exit_code(self):
        result = self._run_application("--version", FAKE_APP_EXIT="7")
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_source_change_builds_new_versioned_environment(self):
        first = self._run("--version")
        source = self.source / "analytics" / "__init__.py"
        source.write_text(source.read_text() + "\n")
        second = self._run("--version")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text().count("uv\tsync"), 2)
        self.assertEqual(len(self._app_environments()), 2)

    def test_configuration_change_builds_new_versioned_environment(self):
        first = self._run("--version")
        config = self.source / "sbk-config.env"
        config.write_text(config.read_text() + "\n# fingerprint regression\n")
        second = self._run("--version")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text().count("uv\tsync"), 2)
        self.assertEqual(len(self._app_environments()), 2)

    def test_moving_checkout_reuses_noneditable_environment(self):
        first = self._run("--version")
        self.assertEqual(first.returncode, 0, first.stderr)
        uv_calls = self.log.read_text().count("uv\t")
        moved_source = self.root / "moved checkout"
        shutil.copytree(self.source, moved_source)
        self.fake_uv.unlink()
        result = subprocess.run(
            [self.bash, str(moved_source / "sbk-analytics.sh"), "--version"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().count("uv\t"), uv_calls)
        self.assertEqual(len(self._app_environments()), 1)

    def test_corrupt_environment_is_repaired(self):
        first = self._run("--version")
        self.assertEqual(first.returncode, 0, first.stderr)
        environment = self._app_environments()[0]
        (environment / "bin" / "python").unlink()
        second = self._run("--version")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text().count("uv\tsync"), 2)

    def test_concurrent_first_runs_publish_one_environment(self):
        env = self.env.copy()
        env["FAKE_UV_SYNC_DELAY"] = "1"
        command = [self.bash, str(self.launcher), "--version"]
        first = subprocess.Popen(
            command, cwd=self.root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        second = subprocess.Popen(
            command, cwd=self.root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        first_output = first.communicate(timeout=30)
        second_output = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_output[1])
        self.assertEqual(second.returncode, 0, second_output[1])
        self.assertEqual(self.log.read_text().count("uv\tsync"), 1)
        self.assertEqual(len(self._app_environments()), 1)

    def test_interrupted_bootstrap_is_recoverable(self):
        env = self.env.copy()
        env["FAKE_UV_SYNC_DELAY"] = "10"
        process = subprocess.Popen(
            [self.bash, str(self.launcher), "--version"],
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.log.exists() and "uv\tsync" in self.log.read_text():
                break
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=15)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM)
        retry = self._run("--version")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        state = self.root / "managed state"
        self.assertFalse(list((state / "app").glob(".*.install-*")))
        self.assertFalse(list((state / "locks").glob("*.lock")))

    def test_uv_archive_checksum_mismatch_is_rejected(self):
        archive = self.root / "invalid-uv.tar.gz"
        archive.write_bytes(b"not the official archive")
        fake_curl = self.bin_dir / "curl"
        fake_curl.write_text(
            """#!/usr/bin/env bash
set -u
printf 'curl' >>"$FAKE_LOG"
printf '\t%s' "$@" >>"$FAKE_LOG"
printf '\n' >>"$FAKE_LOG"
destination=''
previous=''
for argument in "$@"; do
    if [[ "$previous" == '--output' ]]; then destination="$argument"; fi
    previous="$argument"
done
[[ -n "$destination" ]] || exit 2
cp "$FAKE_UV_ARCHIVE" "$destination"
""",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        env = self.env.copy()
        env.pop("SBK_ANALYTICS_UV_EXECUTABLE")
        env["SBK_ANALYTICS_ENV_HOME"] = str(self.root / "checksum state")
        env["FAKE_UV_ARCHIVE"] = str(archive)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            [self.bash, str(self.launcher), "--version"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        curl_call = next(
            line for line in self.log.read_text().splitlines()
            if line.startswith("curl\t")
        )
        self.assertIn("\t--insecure\t", f"\t{curl_call}\t")

    def test_invalid_bootstrap_policy_fails_with_clear_error(self):
        policy_path = self.source / "sbk-bootstrap.env"
        policy_path.write_text(policy_path.read_text().replace(
            "SBK_ANALYTICS_PYTHON_VERSION=3.12.12",
            "SBK_ANALYTICS_PYTHON_VERSION=latest",
        ))
        result = self._run("--version")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid bootstrap policy", result.stderr)
        self.assertIn("SBK_ANALYTICS_PYTHON_VERSION", result.stderr)

    def test_invalid_bootstrap_tls_policy_fails_with_clear_error(self):
        policy_path = self.source / "sbk-bootstrap.env"
        policy_path.write_text(policy_path.read_text().replace(
            "SBK_ANALYTICS_BOOTSTRAP_TLS_VERIFY=false",
            "SBK_ANALYTICS_BOOTSTRAP_TLS_VERIFY=disabled",
        ))
        result = self._run("--version")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid bootstrap policy", result.stderr)
        self.assertIn("SBK_ANALYTICS_BOOTSTRAP_TLS_VERIFY", result.stderr)


if __name__ == "__main__":
    unittest.main()
