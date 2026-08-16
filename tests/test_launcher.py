import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == "nt", "Bash launcher supports Linux and macOS")
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
            "sbk-analytics.ps1",
            "sbk-bootstrap.env",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
        ):
            shutil.copy2(ROOT / name, self.source / name)
        shutil.copytree(ROOT / "analytics", self.source / "analytics")

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
        banner = self.source / "analytics" / "banner.txt"
        banner.write_text(banner.read_text() + "\n")
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
        mirror = self.root / "mirror" / "0.12.5"
        mirror.mkdir(parents=True)
        machine = platform.machine().lower()
        architecture = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
        platform_target = (
            f"{architecture}-apple-darwin"
            if platform.system() == "Darwin"
            else f"{architecture}-unknown-linux-gnu"
        )
        archive = mirror / f"uv-{platform_target}.tar.gz"
        archive.write_bytes(b"not the official archive")
        env = self.env.copy()
        env.pop("SBK_ANALYTICS_UV_EXECUTABLE")
        env["SBK_ANALYTICS_ENV_HOME"] = str(self.root / "checksum state")
        env["SBK_ANALYTICS_UV_BASE_URL"] = mirror.parent.as_uri()
        env["SBK_ANALYTICS_BOOTSTRAP_ALLOW_INSECURE"] = "1"
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


@unittest.skipUnless(os.name == "nt", "PowerShell launcher requires Windows")
class WindowsLauncherTests(unittest.TestCase):
    def test_invalid_bootstrap_policy_fails_with_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher_root = Path(directory)
            launcher = launcher_root / "sbk-analytics.ps1"
            shutil.copy2(ROOT / "sbk-analytics.ps1", launcher)
            policy = (ROOT / "sbk-bootstrap.env").read_text().replace(
                "SBK_ANALYTICS_PYTHON_VERSION=3.12.12",
                "SBK_ANALYTICS_PYTHON_VERSION=latest",
            )
            (launcher_root / "sbk-bootstrap.env").write_text(policy)
            result = subprocess.run(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile",
                    "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(launcher), "--version",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("invalid bootstrap policy", result.stderr)

    def test_real_bootstrap_offline_reuse_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            environment_home = Path(directory) / "managed-state"
            env = os.environ.copy()
            env["SBK_ANALYTICS_ENV_HOME"] = str(environment_home)
            env.pop("VIRTUAL_ENV", None)
            env.pop("CONDA_PREFIX", None)
            base_command = [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "sbk-analytics.ps1"),
            ]
            first = subprocess.run(
                [*base_command, "--version"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("using managed application environment", first.stderr)
            env["SBK_ANALYTICS_BOOTSTRAP_OFFLINE"] = "1"
            second = subprocess.run(
                [*base_command, "deps", "status", "--json"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("sbk", json.loads(second.stdout))
            app_pythons = list((environment_home / "app").glob(
                "*/Scripts/python.exe"
            ))
            self.assertEqual(len(app_pythons), 1)


if __name__ == "__main__":
    unittest.main()
