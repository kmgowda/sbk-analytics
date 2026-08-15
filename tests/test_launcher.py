import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APPLICATION = ROOT / "sbk-analytics"
LAUNCHER = ROOT / "sbk-analytics.sh"
WINDOWS_LAUNCHER = ROOT / "sbk-analytics.ps1"


@unittest.skipIf(os.name == "nt", "launcher supports Linux and macOS")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.log = self.root / "calls.log"
        self.fake_python = self.bin_dir / "python3"
        self.fake_python.write_text(
            """#!/usr/bin/env bash
set -u
{
    printf 'python'
    printf '\t%s' "$@"
    printf '\n'
} >>"$FAKE_LOG"
if [[ "${1:-}" == "-c" ]]; then
    case "${2:-}" in
        *version_info*) exit "${FAKE_PYTHON_INCOMPATIBLE:-0}" ;;
        *hashlib*) printf '%s\\n' "${FAKE_FINGERPRINT:-test-fingerprint}"; exit 0 ;;
        *"import analytics"*) exit 0 ;;
    esac
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
    [[ "${FAKE_VENV_FAIL:-0}" == "1" ]] && exit 1
    mkdir -p "$3/bin"
    cp "$FAKE_PYTHON_TEMPLATE" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "ensurepip" ]]; then
    exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
    if [[ "${FAKE_VENV_PIP_FAIL:-0}" == "1" && "$0" == */.venv/bin/python ]]; then
        exit 1
    fi
    exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "analytics" ]]; then
    shift 2
    printf 'ARG=%s\\n' "$@"
    exit "${FAKE_APP_EXIT:-0}"
fi
exit 1
""",
            encoding="utf-8",
        )
        self.fake_python.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin_dir) + os.pathsep + self.env["PATH"],
            "FAKE_LOG": str(self.log),
            "FAKE_PYTHON_TEMPLATE": str(self.fake_python),
            "SBK_ANALYTICS_ENV_HOME": str(self.root / "environments"),
        })
        self.env.pop("VIRTUAL_ENV", None)
        self.env.pop("CONDA_PREFIX", None)
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash is required for the Unix launcher tests")

    def _run(self, *arguments, **env_updates):
        env = self.env.copy()
        env.update(env_updates)
        return subprocess.run(
            [self.bash, str(LAUNCHER), *arguments],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _run_application(self, *arguments, **env_updates):
        env = self.env.copy()
        env.update(env_updates)
        return subprocess.run(
            [str(APPLICATION), *arguments],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _make_active_environment(self, name):
        environment = self.root / name
        python_bin = environment / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        shutil.copy2(self.fake_python, python_bin)
        python_bin.chmod(0o755)
        return environment

    def _install_fake_conda(self):
        fake_conda = self.bin_dir / "conda"
        fake_conda.write_text(
            """#!/usr/bin/env bash
set -u
printf 'conda' >>"$FAKE_LOG"
printf '\t%s' "$@" >>"$FAKE_LOG"
printf '\n' >>"$FAKE_LOG"
prefix=''
while (($#)); do
    if [[ "$1" == "--prefix" ]]; then
        prefix="$2"
        break
    fi
    shift
done
mkdir -p "$prefix/bin"
cp "$FAKE_PYTHON_TEMPLATE" "$prefix/bin/python"
chmod +x "$prefix/bin/python"
""",
            encoding="utf-8",
        )
        fake_conda.chmod(0o755)

    def test_active_venv_is_reused_and_arguments_are_preserved(self):
        active = self._make_active_environment("active-venv")
        result = self._run(
            "--json", "value with spaces", VIRTUAL_ENV=str(active)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["ARG=--json", "ARG=value with spaces"],
        )
        self.assertIn(f"using venv environment: {active}", result.stderr)
        self.assertNotIn("\t-m\tvenv", self.log.read_text())

    def test_unified_application_delegates_arguments_and_exit_code(self):
        active = self._make_active_environment("unified-active-venv")
        result = self._run_application(
            "--json",
            "value with spaces",
            VIRTUAL_ENV=str(active),
            FAKE_APP_EXIT="7",
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["ARG=--json", "ARG=value with spaces"],
        )
        self.assertIn(f"using venv environment: {active}", result.stderr)

    def test_managed_venv_is_created_then_reused_without_reinstall(self):
        first = self._run("--version")
        second = self._run("--help")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = self.log.read_text()
        self.assertEqual(calls.count("\t-m\tvenv"), 1)
        self.assertEqual(calls.count("\t-m\tpip"), 1)
        self.assertTrue(
            (self.root / "environments" / ".venv" / "bin" / "python").is_file()
        )

    def test_interpreter_fingerprint_change_reinstalls_environment(self):
        first = self._run("--version")
        second = self._run("--version", FAKE_FINGERPRINT="new-interpreter")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text().count("\t-m\tpip"), 2)

    def test_conda_is_used_when_venv_setup_fails(self):
        self._install_fake_conda()
        result = self._run("deps", "status", FAKE_VENV_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("venv setup failed; trying Conda fallback", result.stderr)
        self.assertIn("using conda environment", result.stderr)
        self.assertIn("conda\tcreate\t--yes\t--prefix", self.log.read_text())

    def test_conda_is_used_when_venv_package_install_fails(self):
        self._install_fake_conda()
        result = self._run("--version", FAKE_VENV_PIP_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("venv setup failed; trying Conda fallback", result.stderr)
        self.assertIn("using conda environment", result.stderr)

    def test_application_exit_code_is_preserved(self):
        active = self._make_active_environment("active-conda")
        result = self._run(
            "--version", CONDA_PREFIX=str(active), FAKE_APP_EXIT="7"
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn(f"using conda environment: {active}", result.stderr)


@unittest.skipUnless(os.name == "nt", "PowerShell launcher requires Windows")
class WindowsLauncherTests(unittest.TestCase):
    def test_real_bootstrap_reuse_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            environment_home = Path(directory) / "environments"
            env = os.environ.copy()
            env["SBK_ANALYTICS_ENV_HOME"] = str(environment_home)
            env.pop("VIRTUAL_ENV", None)
            env.pop("CONDA_PREFIX", None)
            base_command = [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WINDOWS_LAUNCHER),
            ]

            first = subprocess.run(
                [*base_command, "--version"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("sbk-analytics", first.stdout)
            self.assertIn("using venv environment", first.stderr)

            second = subprocess.run(
                [*base_command, "deps", "status", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("installing sbk-analytics", second.stderr)
            self.assertIn("sbk", json.loads(second.stdout))
            self.assertTrue(
                (environment_home / ".venv" / "Scripts" / "python.exe").is_file()
            )

            bash = shutil.which("bash")
            self.assertIsNotNone(
                bash,
                "Git Bash is required for the unified Windows application test",
            )
            unified = subprocess.run(
                [bash, str(APPLICATION), "deps", "status", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(unified.returncode, 0, unified.stderr)
            self.assertNotIn("installing sbk-analytics", unified.stderr)
            self.assertIn("sbk", json.loads(unified.stdout))


if __name__ == "__main__":
    unittest.main()
