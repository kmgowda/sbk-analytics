import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from analytics.charts import run_sbk_charts
from analytics.config import OrchestratorConfig
from analytics.processes import (
    ProcessExit,
    _signal_posix_group,
    managed_popen,
    terminate_process,
)
from analytics.releases import ChartsInstall, DependencySource
from analytics.runner import run_jobs


ROOT = Path(__file__).resolve().parent.parent


def _wait_for_file(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _running(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_stopped(*pids: int, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_running(pid) for pid in pids):
            return
        time.sleep(0.05)
    running = [pid for pid in pids if _running(pid)]
    raise AssertionError(f"processes still running after cleanup: {running}")


class ForcedParentExitTests(unittest.TestCase):
    """Exercise the real parent-death protection with a grandchild process."""

    def _launch_controller(self, pid_file: Path) -> subprocess.Popen:
        workload = (
            "import os,pathlib,subprocess,sys,time;"
            "grand=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"pathlib.Path({str(pid_file)!r}).write_text("
            "f'{os.getpid()} {grand.pid}',encoding='utf-8');"
            "time.sleep(60)"
        )
        controller = (
            "import subprocess,sys\n"
            "from analytics.processes import child_process_cleanup,managed_popen\n"
            "with child_process_cleanup():\n"
            f"    p=managed_popen([sys.executable,'-c',{workload!r}],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "    p.wait()\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        kwargs = {
            "cwd": str(ROOT),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        kwargs["start_new_session"] = True
        return subprocess.Popen([sys.executable, "-c", controller], **kwargs)

    def _assert_parent_exit_cleans_tree(self, forced: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "tree.pids"
            controller = self._launch_controller(pid_file)
            child_pid = grand_pid = None
            try:
                _wait_for_file(pid_file)
                child_pid, grand_pid = map(int, pid_file.read_text().split())
                self.assertTrue(_running(child_pid))
                self.assertTrue(_running(grand_pid))
                if forced:
                    controller.kill()
                else:
                    os.kill(controller.pid, signal.SIGTERM)
                controller.wait(timeout=10)
                _wait_stopped(child_pid, grand_pid)
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.wait(timeout=5)
                if child_pid is not None and _running(child_pid):
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigterm_cleans_child_and_grandchild(self):
        self._assert_parent_exit_cleans_tree(forced=False)

    def test_forced_parent_exit_cleans_child_and_grandchild(self):
        self._assert_parent_exit_cleans_tree(forced=True)

    def test_sigterm_cli_json_is_one_document(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = (
                "import pathlib,time\n"
                "from unittest import mock\n"
                "from analytics.cli import main\n"
                f"ready=pathlib.Path({str(ready)!r})\n"
                "def wait_for_signal(*_args, **_kwargs):\n"
                "    ready.write_text('ready', encoding='utf-8')\n"
                "    time.sleep(60)\n"
                "with mock.patch('analytics.cli._execute', "
                "side_effect=wait_for_signal):\n"
                "    raise SystemExit(main(['deps', 'status', '--json']))\n"
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = (
                str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_file(ready)
                os.kill(process.pid, signal.SIGTERM)
                stdout, _stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            payload = json.loads(stdout)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM)
            self.assertEqual(payload["exit_code"], 128 + signal.SIGTERM)
            self.assertEqual(payload["signal"], signal.SIGTERM)


class ManagedProcessTests(unittest.TestCase):
    def test_permission_error_during_group_signal_is_best_effort(self):
        with mock.patch(
            "analytics.processes.os.killpg", side_effect=PermissionError("denied")
        ), self.assertLogs("analytics.processes", level="WARNING") as logs:
            _signal_posix_group(1234, signal.SIGTERM)
        self.assertIn("permission denied", "\n".join(logs.output))

    def test_normal_wrapper_exit_removes_remaining_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "grand.pid"
            code = (
                "import pathlib,subprocess,sys;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid),encoding='utf-8')"
            )
            process = managed_popen([sys.executable, "-c", code])
            self.assertEqual(process.wait(timeout=10), 0)
            _wait_for_file(pid_file)
            _wait_stopped(int(pid_file.read_text()))

    def test_force_kill_escalation_removes_term_resistant_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "tree.pids"
            resistant = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            )
            code = (
                "import os,pathlib,signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"p=subprocess.Popen([sys.executable,'-c',{resistant!r}]);"
                f"pathlib.Path({str(pid_file)!r}).write_text("
                "f'{os.getpid()} {p.pid}',encoding='utf-8');time.sleep(60)"
            )
            process = managed_popen([sys.executable, "-c", code])
            _wait_for_file(pid_file)
            child_pid, grand_pid = map(int, pid_file.read_text().split())
            terminate_process(process, grace_s=0.1)
            _wait_stopped(child_pid, grand_pid)


class WorkloadWiringTests(unittest.TestCase):
    def _runner_interrupt(self, wrapper: str, mode: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "sbk"
            executable.touch()
            yml = root / "job.yml"
            yml.write_text(f"{wrapper}: {{}}\n", encoding="utf-8")
            process = mock.Mock(pid=1234, returncode=None, stdout=None)
            process.poll.return_value = None
            with mock.patch(
                "analytics.runner.managed_popen", return_value=process
            ), mock.patch(
                "analytics.runner._terminate_sbk_process"
            ) as terminate:
                if mode == "serial":
                    target = mock.patch(
                        "analytics.runner._hung_jvm_watchdog",
                        side_effect=ProcessExit(signal.SIGTERM),
                    )
                else:
                    target = mock.patch(
                        "analytics.runner.time.sleep",
                        side_effect=ProcessExit(signal.SIGTERM),
                    )
                with target, self.assertRaises(ProcessExit):
                    run_jobs(
                        executable,
                        [("job", yml, root / "job.csv")],
                        mode=mode,
                        log_dir=root / "logs",
                    )
            terminate.assert_called_once_with(
                process, yml, is_gem=wrapper == "sbkGemArgs"
            )

    def test_serial_sbk_interrupt_terminates_tree(self):
        self._runner_interrupt("sbkArgs", "serial")

    def test_serial_sbk_gem_interrupt_terminates_tree(self):
        self._runner_interrupt("sbkGemArgs", "serial")

    def test_parallel_sbk_interrupt_terminates_tree(self):
        self._runner_interrupt("sbkArgs", "parallel")

    def test_charts_interrupt_terminates_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = mock.Mock()
            process.wait.side_effect = ProcessExit(signal.SIGTERM)
            install = ChartsInstall(
                root, DependencySource.LOCAL, _cli=root / "sbk-charts"
            )
            with mock.patch(
                "analytics.charts.managed_popen", return_value=process
            ), mock.patch(
                "analytics.charts.terminate_process"
            ) as terminate, self.assertRaises(ProcessExit):
                run_sbk_charts(
                    install,
                    OrchestratorConfig(),
                    [root / "input.csv"],
                    root / "output.xlsx",
                    work_dir=root,
                )
            terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
