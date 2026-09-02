import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from analytics.policy import RUNTIME_POLICY
from analytics.workflow import (
    _collect_extra_csvs,
    _complete_dependency_check,
    _validate_usable_inputs,
)


class WorkflowPhaseTests(unittest.TestCase):
    def test_dependency_check_isolated_phase_resolves_charts_and_emits_summary(self):
        charts = object()
        services = SimpleNamespace(
            ensure_sbk_charts=mock.Mock(return_value=charts),
            _print_charts_resolution=mock.Mock(),
            _dependency_summary=mock.Mock(return_value={"dependencies": {}}),
            _emit_json=mock.Mock(),
        )
        args = SimpleNamespace(
            command=RUNTIME_POLICY.cli.dependencies_command,
            subcommand=RUNTIME_POLICY.cli.doctor_subcommand,
        )
        versions = SimpleNamespace(
            sbk_charts="4.26.7.1",
            sbk_charts_url="https://example.invalid/charts",
            sbk_charts_sha256=None,
            downloads_folder=None,
            sbk_charts_local_folder=None,
            sbk_charts_local_executable=None,
            sbk_charts_version_policy="warn",
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = _complete_dependency_check(
                args, object(), versions, False, services, io.StringIO()
            )

        self.assertEqual(result, RUNTIME_POLICY.exit_codes.success)
        self.assertTrue(services.ensure_sbk_charts.call_args.kwargs["preflight"])
        services._print_charts_resolution.assert_called_once_with(
            charts, versions.sbk_charts
        )
        services._emit_json.assert_called_once()

    def test_collect_extra_csvs_accepts_relative_nonempty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            csv_path = work / "existing.csv"
            csv_path.write_text("header\nvalue\n", encoding="utf-8")
            cfg = SimpleNamespace(use_files=["existing.csv", "missing.csv"])

            with contextlib.redirect_stderr(io.StringIO()):
                result = _collect_extra_csvs(cfg, work)

            self.assertEqual(result, [csv_path.resolve()])

    def test_no_usable_csv_phase_returns_before_charts(self):
        services = SimpleNamespace(
            _dependency_summary_sbk=mock.Mock(return_value={"source": "LOCAL"}),
            _emit_json=mock.Mock(),
        )
        failed = [SimpleNamespace(class_name="failed")]

        with contextlib.redirect_stderr(io.StringIO()):
            result = _validate_usable_inputs(
                [], failed, [], object(), services, io.StringIO()
            )

        self.assertEqual(result, RUNTIME_POLICY.exit_codes.no_usable_csv)
        payload = services._emit_json.call_args.args[1]
        self.assertEqual(payload[RUNTIME_POLICY.diagnostics.failed_instances], ["failed"])

    def test_partial_failure_phase_reports_total_without_exiting(self):
        services = SimpleNamespace(
            _dependency_summary_sbk=mock.Mock(),
            _emit_json=mock.Mock(),
        )
        succeeded = [SimpleNamespace(class_name="passed")]
        failed = [SimpleNamespace(class_name="failed")]
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = _validate_usable_inputs(
                succeeded, failed, [], object(), services, io.StringIO()
            )

        self.assertIsNone(result)
        self.assertIn("1 of 2 SBK runs failed", stderr.getvalue())
        services._emit_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
