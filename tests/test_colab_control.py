import contextlib
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytest

# The Google Colab control SDK supports Python 3.12+ only; CI installs it on the 3.12 job.
pytest.importorskip("colab_cli")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_actions as control
import colab_remote_job as remote_job

REQUEST = {
    "REQUEST_ID": "a" * 32,
    "CLIPFORGE_SOURCE_REF": "b" * 40,
    "INPUT_TAG": "clipforge-input-" + "a" * 32,
}
AUDIO_URL = (
    "https://kick.com/test_channel-1/videos/01234567-89ab-cdef-0123-456789abcdef"
)


def granted_assignment(endpoint="owned-session"):
    from colab_cli.client import Assignment

    return Assignment(
        endpoint=endpoint,
        runtimeProxyInfo={
            "url": "https://test.invalid",
            "token": "test-only",
            "tokenExpiresInSeconds": 3600,
        },
    )


def unassigned_response():
    from colab_cli.client import GetAssignmentResponse, Variant

    return GetAssignmentResponse(
        acc="NONE", nbh="synthetic-notebook", token="test-only", variant=Variant.DEFAULT
    )


class ControlTests(unittest.TestCase):
    def test_missing_oauth_stops_before_any_allocation(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {**REQUEST, "COLAB_ADC_JSON": ""}),
            patch.object(control, "authenticated_client") as authenticate,
        ):
            with self.assertRaisesRegex(control.ControlError, "one-time"):
                control.Controller(temporary).start()
            authenticate.assert_not_called()
            self.assertFalse((Path(temporary) / "state.json").exists())

    def test_wrong_account_stops_before_constructing_colab_client(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "email": "different@example.com",
            "verified_email": True,
        }
        session = Mock()
        session.get.return_value = response
        with (
            patch.object(control, "credentials_from_info", return_value=object()),
            patch(
                "google.auth.transport.requests.AuthorizedSession", return_value=session
            ),
            patch("colab_cli.client.Client") as client,
        ):
            with self.assertRaisesRegex(control.ControlError, "mismatch"):
                control.authenticated_client({"type": "authorized_user"})
            client.assert_not_called()

    def test_service_account_cannot_replace_colab_pro_user(self):
        with self.assertRaisesRegex(control.ControlError, "end-user"):
            control.credentials_from_info({"type": "service_account"})

    def test_owned_runtime_is_persisted_then_only_that_runtime_is_released(self):
        from colab_cli.client import Accelerator, Variant

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    **REQUEST,
                    "COLAB_ADC_JSON": json.dumps(
                        {"type": "authorized_user", "refresh_token": "test-only"}
                    ),
                },
            ),
        ):
            client = Mock()
            client.list_assignments.side_effect = [
                [],
                [SimpleNamespace(endpoint="someone-elses-session")],
            ]
            client.assign.return_value = SimpleNamespace(
                endpoint="owned-session",
                runtime_proxy_info=SimpleNamespace(
                    url="https://test.invalid", token="test-only-proxy"
                ),
            )
            with patch.object(control, "authenticated_client", return_value=client):
                controller = control.Controller(temporary)
                controller.start()
                recorded = controller.state()
                self.assertEqual(recorded["endpoint"], "owned-session")
                self.assertEqual(controller.state_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    client.assign.call_args.kwargs["accelerator"], Accelerator.T4
                )
                self.assertEqual(client.assign.call_args.kwargs["variant"], Variant.GPU)
                controller.stop()
                client.unassign.assert_called_once_with("owned-session")
                self.assertEqual(controller.state()["status"], "stopped")
                self.assertNotIn("token", controller.state())
                self.assertFalse(controller.credentials_path.exists())

    def test_compute_units_are_recorded_around_the_job_without_failing_it(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ, {**REQUEST, "COLAB_ADC_JSON": '{"type":"authorized_user"}'}
            ),
            patch("builtins.print") as printed,
        ):
            client = Mock()
            client.list_assignments.return_value = []
            client.get_consumption_user_info.side_effect = [
                SimpleNamespace(
                    paid_compute_units_balance=180.0,
                    consumption_rate_hourly=0.0,
                    assignments_count=0,
                ),
                SimpleNamespace(
                    paid_compute_units_balance=179.6,
                    consumption_rate_hourly=0.0,
                    assignments_count=0,
                ),
            ]
            client.assign.return_value = SimpleNamespace(
                endpoint="owned-session",
                runtime_proxy_info=SimpleNamespace(
                    url="https://test.invalid", token="t"
                ),
            )
            with patch.object(control, "authenticated_client", return_value=client):
                controller = control.Controller(temporary)
                controller.start()
                self.assertEqual(controller.state()["units_before"]["balance"], 180.0)
                controller.stop()
            self.assertEqual(controller.state()["units_after"]["balance"], 179.6)
            lines = " ".join(str(call.args[0]) for call in printed.call_args_list)
            self.assertIn(
                "Compute units used while this job ran (account-wide): 0.40", lines
            )
        client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        self.assertIsNone(control.compute_units(client))

    def test_shared_balance_is_not_reported_as_this_runtime_use(self):
        for shared, active, expected in (
            ("1", 0, "changed account-wide by 0.18 (0 other runtime(s) still active)"),
            ("", 1, "changed account-wide by 0.18 (1 other runtime(s) still active)"),
            ("", 0, "Compute units used while this job ran (account-wide): 0.18"),
        ):
            with (
                self.subTest(shared=shared, active=active),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    os.environ,
                    {
                        **REQUEST,
                        "COLAB_ADC_JSON": '{"type":"authorized_user"}',
                        "CLIPFORGE_SHARED_BALANCE": shared,
                    },
                ),
                patch("builtins.print") as printed,
            ):
                client = Mock()
                client.list_assignments.return_value = []
                client.get_consumption_user_info.side_effect = [
                    SimpleNamespace(
                        paid_compute_units_balance=184.11,
                        consumption_rate_hourly=0.0,
                        assignments_count=0,
                    ),
                    SimpleNamespace(
                        paid_compute_units_balance=183.93,
                        consumption_rate_hourly=1.07 * active,
                        assignments_count=active,
                    ),
                ]
                client.assign.return_value = SimpleNamespace(
                    endpoint="owned-session",
                    runtime_proxy_info=SimpleNamespace(
                        url="https://test.invalid", token="t"
                    ),
                )
                with patch.object(control, "authenticated_client", return_value=client):
                    controller = control.Controller(temporary)
                    controller.start()
                    controller.stop()
                state = controller.state()
                self.assertGreaterEqual(state["released_at"], state["allocated_at"])
                lines = " ".join(str(call.args[0]) for call in printed.call_args_list)
                self.assertIn(expected, lines)
                self.assertIn("Runtime held: T4 ", lines)
                if expected.startswith("changed"):
                    self.assertNotIn("used while this job ran", lines)

    def test_stage_line_names_the_cores_that_did_the_work(self):
        self.assertEqual(
            control.host_text({"cpu_count": 8, "memory_gib": 51.0}),
            " host: 8 cores, 51.0 GiB",
        )
        self.assertEqual(control.host_text(None), "")
        self.assertEqual(control.host_text({"cpu_count": "8"}), "")

    def test_zero_allocation_wait_does_not_retry_or_leave_credentials(self):
        from colab_cli.client import TooManyAssignmentsError

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    **REQUEST,
                    "COLAB_ADC_JSON": '{"type":"authorized_user"}',
                    "CLIPFORGE_COLAB_POOL": "",
                    "CLIPFORGE_ALLOCATION_WAIT_SECONDS": "0",
                },
            ),
            patch.object(control.time, "sleep") as sleep,
        ):
            client = Mock()
            client.list_assignments.return_value = []
            client.assign.side_effect = TooManyAssignmentsError()
            with patch.object(control, "authenticated_client", return_value=client):
                controller = control.Controller(temporary)
                with self.assertRaisesRegex(control.ControlError, "refused"):
                    controller.start()
                self.assertEqual(client.assign.call_count, 3)
                sleep.assert_not_called()
                self.assertEqual(controller.state()["status"], "stopped")
                self.assertFalse(controller.credentials_path.exists())
                client.unassign.assert_not_called()

    def test_stop_failure_is_reported_and_retains_ownership_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = control.Controller(temporary)
            control.write_private(
                controller.state_path,
                {
                    "status": "allocated",
                    "endpoint": "owned-session",
                    "url": "https://test.invalid",
                    "token": "test-only",
                },
            )
            client = Mock()
            client.unassign.side_effect = RuntimeError("transport failure")
            with patch.object(controller, "client", return_value=client):
                with self.assertRaises(RuntimeError):
                    controller.stop()
            self.assertEqual(controller.state()["endpoint"], "owned-session")
            self.assertEqual(controller.state()["status"], "allocated")

    def _pipeline_controller(self, temporary, restore, execute=None):
        controller = control.Controller(Path(temporary) / "state")
        control.write_private(
            controller.state_path,
            {
                "status": "allocated",
                "mode": "asr",
                "request_id": "a" * 32,
                "source_ref": "b" * 40,
                "input_tag": "clipforge-input-" + "a" * 32,
                "endpoint": "owned-session",
                "url": "https://test.invalid",
                "token": "test-only",
            },
        )
        events = []
        bundles = Mock()
        bundles.restore.side_effect = lambda *a, **k: (
            events.append("restore"),
            restore(*a, **k),
        )[1]
        patches = [
            patch.dict(
                os.environ, {"GH_TOKEN": "test-only", "GEMINI_API_KEY": "test-only"}
            ),
            patch("colab_cli.contents.ContentsClient"),
            patch.object(control, "runtime_from_state"),
            patch.object(control, "ReleaseBundles", return_value=bundles),
            patch.object(
                control,
                "checked_execute",
                side_effect=execute or (lambda *a, **k: []),
            ),
            patch.object(
                controller,
                "stop",
                side_effect=lambda: events.append("stop"),
            ),
        ]
        return controller, events, patches

    @staticmethod
    def _result_restore(status, stage, error_type=None, timings=None):
        def restore(tag, destination, identity, required=False):
            destination = Path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "request-receipt.json").write_text(
                json.dumps(
                    {
                        "request_id": "a" * 32,
                        "source_ref": "b" * 40,
                        "status": status,
                    }
                )
            )
            (destination / "manifest.json").write_text(json.dumps({"status": status}))
            (destination / "gpu-receipt.json").write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "error_type": error_type,
                        "timings": timings or {},
                    }
                )
            )
            return True

        return restore

    def test_pipeline_releases_t4_before_results_are_retrieved(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, events, patches = self._pipeline_controller(
                temporary,
                self._result_restore(
                    "asr_part_complete", "complete", timings={"pipeline": 1.5}
                ),
            )
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    controller.run(Path(temporary) / "output")
            self.assertEqual(events, ["stop", "restore"])

    def test_failed_execution_deletes_remote_payload_once_before_stopping(self):
        for cleanup_fails in (False, True):
            with (
                self.subTest(cleanup_fails=cleanup_fails),
                tempfile.TemporaryDirectory() as temporary,
            ):
                original = RuntimeError("synthetic kernel failure")
                calls = []

                def execute(runtime, code, timeout):
                    calls.append(code)
                    if len(calls) == 1:
                        events.append("execute")
                        raise original
                    events.append("delete-payload")
                    if cleanup_fails:
                        raise RuntimeError("synthetic cleanup failure")
                    return []

                controller, events, patches = self._pipeline_controller(
                    temporary, self._result_restore("failed", "render"), execute
                )
                runtime = Mock()
                runtime.stop.side_effect = lambda: events.append("disconnect")
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    stack.enter_context(
                        patch.object(
                            control, "runtime_from_state", return_value=runtime
                        )
                    )
                    stack.enter_context(contextlib.redirect_stdout(stdout))
                    stack.enter_context(contextlib.redirect_stderr(stderr))
                    with self.assertRaises(RuntimeError) as raised:
                        controller.run(Path(temporary) / "output")
                self.assertIs(raised.exception, original)
                self.assertEqual(
                    events,
                    ["execute", "delete-payload", "disconnect", "stop", "restore"],
                )
                self.assertEqual(len(calls), 2)
                self.assertIn(repr(control.REMOTE_PAYLOAD_PATH), calls[1])
                self.assertIn("unlink(missing_ok=True)", calls[1])
                self.assertNotIn(
                    "test-only", calls[1] + stdout.getvalue() + stderr.getvalue()
                )
                self.assertFalse((controller.root / "job-payload.json").exists())

    def test_audio_analysis_needs_no_gemini_key_and_collects_topics(self):
        for key in ("", "test-only"):
            with (
                self.subTest(key_present=bool(key)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                restore_complete = self._result_restore("asr_part_complete", "complete")

                def restore(*args, **kwargs):
                    restore_complete(*args, **kwargs)
                    receipt_path = Path(args[1]) / "request-receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    receipt["audio_source_url"] = AUDIO_URL
                    receipt_path.write_text(json.dumps(receipt))

                controller, events, patches = self._pipeline_controller(
                    temporary, restore
                )
                uploaded = []

                def upload(local, remote):
                    uploaded.append(json.loads(Path(local).read_text()))

                with contextlib.ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    stack.enter_context(
                        patch.dict(
                            os.environ,
                            {
                                "GEMINI_API_KEY": key,
                                "YOUTUBE_COOKIES_B64": "private-cookie-test",
                            },
                        )
                    )
                    contents = stack.enter_context(
                        patch("colab_cli.contents.ContentsClient")
                    )
                    contents.return_value.upload.side_effect = upload
                    controller.run(Path(temporary) / "output")
                self.assertNotIn("audio_url", uploaded[0])
                self.assertNotIn("youtube_cookies_b64", uploaded[0])
                self.assertNotIn("private-cookie-test", json.dumps(uploaded))
                self.assertNotIn("gemini_api_key", uploaded[0])
                self.assertEqual(events, ["stop", "restore"])

    def test_stage_timings_are_printed_from_the_returned_receipt(self):
        timings = {"cache_restore": 0.25, "setup": 12.5, "pipeline": 300.75}
        with tempfile.TemporaryDirectory() as temporary:
            controller, events, patches = self._pipeline_controller(
                temporary,
                self._result_restore("asr_part_complete", "complete", timings=timings),
            )
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    controller.run(Path(temporary) / "output")
            self.assertIn(
                "Colab stage: complete error: None timings: " + str(timings),
                stdout.getvalue(),
            )

    def test_failed_pipeline_still_prints_stage_and_error_class_only(self):
        timings = {"pipeline": 42.0}
        with tempfile.TemporaryDirectory() as temporary:
            controller, events, patches = self._pipeline_controller(
                temporary,
                self._result_restore(
                    "failed", "pipeline", error_type="RuntimeError", timings=timings
                ),
                execute=lambda *a, **k: [],
            )
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaisesRegex(control.ControlError, "partial"):
                        controller.run(Path(temporary) / "output")
            self.assertEqual(events, ["stop", "restore"])
            self.assertIn(
                "Colab stage: pipeline error: RuntimeError timings: " + str(timings),
                stdout.getvalue(),
            )

    def test_report_stages_prints_only_the_worker_error_type(self):
        message = "clip failed the quality gate: captions.srt and captions.ass differ"
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "gpu-receipt.json").write_text(
                json.dumps(
                    {
                        "stage": "render",
                        "error_type": "RuntimeError",
                        "timings": {"render": 3.0},
                        "worker_error": {
                            "error_type": "PipelineError",
                            "message": message,
                        },
                    }
                )
            )
            controller = control.Controller(temporary)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                controller.report_stages(temporary)
        self.assertEqual(
            stdout.getvalue().splitlines()[1],
            "Worker error: PipelineError",
        )
        self.assertNotIn(message, stdout.getvalue())

    def test_report_stages_without_worker_error_prints_only_the_stage_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "gpu-receipt.json").write_text(
                json.dumps({"stage": "complete", "error_type": None})
            )
            controller = control.Controller(temporary)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                controller.report_stages(temporary)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertNotIn("Worker error", stdout.getvalue())

    def test_report_stages_never_prints_private_ascii_log_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "gpu-receipt.json").write_text(
                json.dumps(
                    {
                        "stage": "prefetch",
                        "error_type": "RuntimeError",
                        "log_tail": ["private-english-dialogue-and-secret"],
                    }
                )
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                control.Controller(temporary).report_stages(temporary)
        self.assertNotIn("private-english-dialogue-and-secret", stdout.getvalue())
        self.assertIn("private result", stdout.getvalue())

    def test_worker_error_keeps_type_and_bounded_message_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stage-error.json"
            path.write_text(
                json.dumps(
                    {
                        "stage": "render",
                        "error_type": "PipelineError",
                        "message": "x" * 500,
                    }
                )
            )
            error = remote_job.worker_error(path)
            self.assertEqual(error["error_type"], "PipelineError")
            self.assertEqual(len(error["message"]), 300)
            path.write_text(json.dumps({"error_type": "OSError", "message": 3}))
            self.assertEqual(
                remote_job.worker_error(path),
                {"error_type": "OSError", "message": ""},
            )
            path.write_text("[1, 2]")
            self.assertIsNone(remote_job.worker_error(path))
            self.assertIsNone(remote_job.worker_error(Path(temporary) / "missing.json"))

    def test_early_release_failure_still_retrieves_results_and_fails_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, events, patches = self._pipeline_controller(
                temporary,
                self._result_restore("asr_part_complete", "complete"),
            )
            patches[-1] = patch.object(
                controller,
                "stop",
                side_effect=control.ControlError("release unconfirmed"),
            )
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(control.ControlError, "not confirmed"):
                        controller.run(Path(temporary) / "output")
            self.assertEqual(events, ["restore"])

    def test_second_stop_after_release_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = control.Controller(temporary)
            control.write_private(controller.state_path, {"status": "stopped"})
            client = Mock()
            with patch.object(controller, "client", return_value=client):
                controller.stop()
                controller.stop()
            client.unassign.assert_not_called()
            client.list_assignments.assert_not_called()

    def test_bootstrap_clones_with_blobless_filter(self):
        source = inspect.getsource(control.Controller.run)
        self.assertIn("'--filter=blob:none'", source)

    def test_stage_timer_records_rounded_seconds_even_when_stage_fails(self):
        timings = {}
        with self.assertRaises(RuntimeError):
            with remote_job.timed(timings, "setup"):
                raise RuntimeError("stage failed")
        self.assertIn("setup", timings)
        self.assertIsInstance(timings["setup"], float)
        self.assertEqual(timings["setup"], round(timings["setup"], 2))


if __name__ == "__main__":
    unittest.main()


class ProbeTests(unittest.TestCase):
    def run_probe(self, assign, lookup=None, expected_error=None):
        client = Mock()
        client.assign.side_effect = assign
        if isinstance(lookup, Exception):
            client._get_assignment.side_effect = lookup
        else:
            client._get_assignment.return_value = lookup
        client.list_assignments.return_value = []
        client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        with (
            patch.dict(os.environ, {"COLAB_ADC_JSON": '{"type":"authorized_user"}'}),
            patch.object(control, "authenticated_client", return_value=client),
            patch("builtins.print") as printed,
        ):
            if expected_error is None:
                control.probe("L4")
            else:
                with self.assertRaises(expected_error):
                    control.probe("L4")
        return client, " ".join(str(c.args[0]) for c in printed.call_args_list)

    def test_a_granted_runtime_is_released_at_once(self):
        granted = SimpleNamespace(endpoint="probe-session")
        client, lines = self.run_probe(lambda *a, **k: granted)
        client.unassign.assert_called_once_with("probe-session")
        self.assertIn("Probe L4/STANDARD: granted", lines)
        self.assertIn("released", lines)

    def test_a_held_probe_reports_its_own_rate_then_releases(self):
        client = Mock()
        client.assign.return_value = SimpleNamespace(endpoint="probe-session")
        client.list_assignments.return_value = []
        client.get_consumption_user_info.side_effect = [
            SimpleNamespace(
                paid_compute_units_balance=180.0,
                consumption_rate_hourly=1.07,
                assignments_count=1,
            ),
            SimpleNamespace(
                paid_compute_units_balance=179.9,
                consumption_rate_hourly=2.9,
                assignments_count=2,
            ),
        ]
        with (
            patch.dict(os.environ, {"COLAB_ADC_JSON": '{"type":"authorized_user"}'}),
            patch.object(control, "authenticated_client", return_value=client),
            patch.object(control.time, "sleep"),
            patch("builtins.print") as printed,
        ):
            control.probe("L4", 20)
        lines = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("Probe L4/STANDARD: rate rise 1.83/h", lines)
        client.unassign.assert_called_once_with("probe-session")

    def test_a_refusal_allocates_nothing(self):
        from colab_cli.client import TooManyAssignmentsError

        def refuse(*args, **kwargs):
            raise TooManyAssignmentsError()

        client, lines = self.run_probe(refuse)
        client.unassign.assert_not_called()
        client._get_assignment.assert_not_called()
        self.assertIn("refused (TooManyAssignmentsError)", lines)

    def test_lost_reply_reconciles_and_releases_only_the_owned_probe(self):
        client, lines = self.run_probe(
            RuntimeError("synthetic reply loss"),
            lookup=granted_assignment("probe-session"),
            expected_error=RuntimeError,
        )
        client.unassign.assert_called_once_with("probe-session")
        self.assertEqual(
            client._get_assignment.call_args.args,
            (client.assign.call_args.args[0], *client.assign.call_args.kwargs.values()),
        )
        self.assertIn("released", lines)
        self.assertNotIn("refused", lines)

    def test_http_errors_are_reconciled_and_never_reported_as_refusals(self):
        from colab_cli.client import ColabRequestError

        for status in (403, 429):
            with self.subTest(status=status):
                error = ColabRequestError(
                    "synthetic HTTP failure", None, SimpleNamespace(status_code=status)
                )
                client, lines = self.run_probe(
                    error,
                    lookup=unassigned_response(),
                    expected_error=ColabRequestError,
                )
                client._get_assignment.assert_called_once()
                client.unassign.assert_not_called()
                self.assertNotIn("refused", lines)

    def test_failed_probe_reconciliation_requires_manual_confirmation(self):
        client, lines = self.run_probe(
            RuntimeError("synthetic reply loss"),
            lookup=RuntimeError("synthetic lookup failure"),
            expected_error=control.ControlError,
        )
        client.unassign.assert_not_called()
        self.assertNotIn("refused", lines)


class LostAllocationReplyTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch.object(control.time, "sleep"))

    def start_with_failed_assign(self, lookup, error=None):
        client = Mock()
        client.list_assignments.side_effect = [
            [SimpleNamespace(endpoint="earlier")],
            [
                SimpleNamespace(endpoint="earlier"),
                SimpleNamespace(endpoint="other-job"),
            ],
        ]
        error = error or RuntimeError("synthetic reply loss")
        if isinstance(lookup, (Exception, list)):
            client._get_assignment.side_effect = lookup
        else:
            client._get_assignment.return_value = lookup
        client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        def assign(notebook_hash, **kwargs):
            self.assertEqual(controller.state()["notebook_hash"], str(notebook_hash))
            self.assertEqual(controller.state()["status"], "creating")
            raise error

        client.assign.side_effect = assign
        with (
            patch.dict(
                os.environ,
                {
                    **REQUEST,
                    "COLAB_ADC_JSON": '{"type":"authorized_user"}',
                    "CLIPFORGE_COLAB_POOL": "CPU:high-ram,T4",
                },
            ),
            patch.object(control, "authenticated_client", return_value=client),
        ):
            controller = control.Controller(temporary.name)
            with self.assertRaises(type(error)) as raised:
                controller.start()
            self.assertIs(raised.exception, error)
        self.assertEqual(client.assign.call_count, 1)
        self.assertEqual(
            client._get_assignment.call_args.args,
            (client.assign.call_args.args[0], *client.assign.call_args.kwargs.values()),
        )
        return controller, client

    def test_nothing_assigned_is_recorded_as_stopped_without_credentials(self):
        controller, client = self.start_with_failed_assign(unassigned_response())
        self.assertEqual(client._get_assignment.call_count, 3)
        self.assertEqual(
            [call.args for call in self.sleep.call_args_list], [(10,), (10,)]
        )
        self.assertEqual(controller.state()["status"], "stopped")
        self.assertNotIn("token", controller.state())
        self.assertFalse(controller.credentials_path.exists())

    def test_delayed_assignment_is_recorded_before_absence_is_confirmed(self):
        controller, client = self.start_with_failed_assign(
            [unassigned_response(), unassigned_response(), granted_assignment("ours")]
        )
        self.assertEqual(controller.state()["status"], "allocated")
        self.assertEqual(controller.state()["endpoint"], "ours")
        self.assertTrue(controller.credentials_path.exists())
        self.assertEqual(client._get_assignment.call_count, 3)
        self.assertEqual(
            [call.args for call in self.sleep.call_args_list], [(10,), (10,)]
        )

    def test_exception_after_absence_keeps_uncertain_and_credentials(self):
        controller, client = self.start_with_failed_assign(
            [unassigned_response(), RuntimeError("synthetic lookup failure")]
        )
        self.assertEqual(controller.state()["status"], "uncertain")
        self.assertTrue(controller.credentials_path.exists())
        self.assertEqual(client._get_assignment.call_count, 2)

    def test_stop_waits_for_delayed_assignment_and_releases_it(self):
        controller, client = self.start_with_failed_assign(
            RuntimeError("lookup failed")
        )
        client._get_assignment.side_effect = [
            unassigned_response(),
            granted_assignment("ours"),
        ]
        with patch.object(controller, "client", return_value=client):
            controller.stop()
        client.unassign.assert_called_once_with("ours")
        self.assertEqual(controller.state()["status"], "stopped")

    def test_notebook_assignment_is_kept_with_proxy_details_for_release(self):
        controller, client = self.start_with_failed_assign(granted_assignment("ours"))
        self.assertEqual(controller.state()["status"], "allocated")
        self.assertEqual(controller.state()["endpoint"], "ours")
        self.assertEqual(controller.state()["url"], "https://test.invalid")
        self.assertEqual(controller.state()["token"], "test-only")
        self.assertTrue(controller.credentials_path.exists())
        with patch.object(controller, "client", return_value=client):
            controller.stop()
        client.unassign.assert_called_once_with("ours")

    def test_another_jobs_new_endpoint_is_never_claimed_or_released(self):
        controller, client = self.start_with_failed_assign(unassigned_response())
        controller.stop()
        self.assertNotIn("endpoint", controller.state())
        client.list_assignments.assert_called_once()
        client.unassign.assert_not_called()

    def test_failed_reconciliation_keeps_uuid_and_credentials_as_uncertain(self):
        controller, client = self.start_with_failed_assign(
            RuntimeError("lookup failed")
        )
        self.assertEqual(controller.state()["status"], "uncertain")
        self.assertEqual(
            controller.state()["notebook_hash"], str(client.assign.call_args.args[0])
        )
        self.assertTrue(controller.credentials_path.exists())
        client.unassign.assert_not_called()

    def test_http_failures_never_move_from_cpu_to_a_costlier_candidate(self):
        from colab_cli.client import Accelerator, ColabRequestError

        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                error = ColabRequestError(
                    "synthetic HTTP failure", None, SimpleNamespace(status_code=status)
                )
                controller, client = self.start_with_failed_assign(
                    unassigned_response(), error
                )
                self.assertEqual(
                    client.assign.call_args.kwargs["accelerator"], Accelerator.NONE
                )
                self.assertEqual(controller.state()["status"], "stopped")

    def test_stop_reconciles_uncertain_and_interrupted_creating_states(self):
        for status in ("uncertain", "creating"):
            for allocated in (False, True):
                with self.subTest(status=status, allocated=allocated):
                    controller, client = self.start_with_failed_assign(
                        RuntimeError("lookup failed")
                    )
                    record = controller.state()
                    record["status"] = status
                    control.write_private(controller.state_path, record)
                    client._get_assignment.side_effect = None
                    client._get_assignment.return_value = (
                        granted_assignment("ours")
                        if allocated
                        else unassigned_response()
                    )
                    with patch.object(controller, "client", return_value=client):
                        controller.stop()
                    self.assertEqual(
                        client._get_assignment.call_args_list[0],
                        client._get_assignment.call_args_list[1],
                    )
                    self.assertEqual(controller.state()["status"], "stopped")
                    self.assertFalse(controller.credentials_path.exists())
                    self.assertNotIn("token", controller.state())
                    if allocated:
                        client.unassign.assert_called_once_with("ours")
                        self.assertEqual(client.list_assignments.call_count, 2)
                    else:
                        client.unassign.assert_not_called()

    def test_stop_failed_reconciliation_keeps_uncertain_state_for_manual_check(self):
        controller, client = self.start_with_failed_assign(
            RuntimeError("lookup failed")
        )
        with patch.object(controller, "client", return_value=client):
            with self.assertRaisesRegex(control.ControlError, "check the account"):
                controller.stop()
        self.assertEqual(controller.state()["status"], "uncertain")
        self.assertIn("notebook_hash", controller.state())
        self.assertTrue(controller.credentials_path.exists())
        client.unassign.assert_not_called()


class PoolTests(unittest.TestCase):
    def test_asr_default_pool_tries_t4_then_l4(self):
        from colab_cli.client import Accelerator, Shape

        with patch.dict(os.environ, {"CLIPFORGE_COLAB_POOL": ""}):
            self.assertEqual(
                control.colab_pool("asr"),
                [
                    (Accelerator.T4, Shape.HIGH_RAM),
                    (Accelerator.T4, Shape.STANDARD),
                    (Accelerator.L4, Shape.STANDARD),
                ],
            )

    def start_stage(self, refusals, pool=None):
        from colab_cli.client import Accelerator, TooManyAssignmentsError

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        review = Path(temporary.name) / "review.json"
        review.write_text('{"clips": []}')
        client = Mock()
        client.list_assignments.return_value = []
        client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        granted = SimpleNamespace(
            endpoint="stage-session",
            runtime_proxy_info=SimpleNamespace(url="https://t.invalid", token="t"),
        )
        calls = []

        def assign(*args, **kwargs):
            calls.append((kwargs["accelerator"], kwargs["shape"].name))
            if len(calls) <= refusals:
                raise TooManyAssignmentsError()
            return granted

        client.assign.side_effect = assign
        environment = {
            **REQUEST,
            "COLAB_ADC_JSON": '{"type":"authorized_user"}',
            "AUDIO_STAGE": "render",
            "PREVIOUS_RUN": "123",
            "PREVIOUS_REQUEST": "c" * 32,
            "CLIP_INDEX": "2",
            "REVIEW_FILE": str(review),
            "CLIPFORGE_ALLOCATION_WAIT_SECONDS": "0",
        }
        if pool:
            environment["CLIPFORGE_COLAB_POOL"] = pool
        with (
            patch.dict(os.environ, environment),
            patch.object(control, "authenticated_client", return_value=client),
        ):
            controller = control.Controller(Path(temporary.name) / "control")
            try:
                controller.start("audio-stage")
            except control.ControlError:
                pass
        return controller, calls, Accelerator

    def test_clip_rendering_requires_gpu_first(self):
        controller, calls, Accelerator = self.start_stage(refusals=0)
        self.assertEqual(calls, [(Accelerator.L4, "STANDARD")])
        state = controller.state()
        self.assertEqual((state["accelerator"], state["shape"]), ("L4", "STANDARD"))
        self.assertEqual(state["stage"]["clip_index"], "2")

    def test_a_412_refusal_moves_to_the_next_runtime_in_the_pool(self):
        controller, calls, Accelerator = self.start_stage(refusals=2)
        self.assertEqual(
            calls,
            [
                (Accelerator.L4, "STANDARD"),
                (Accelerator.T4, "HIGH_RAM"),
                (Accelerator.T4, "STANDARD"),
            ],
        )
        self.assertEqual(controller.state()["status"], "allocated")

    def test_a_pool_refused_everywhere_holds_nothing(self):
        controller, calls, _ = self.start_stage(refusals=9, pool="CPU:high-ram,L4")
        self.assertEqual(len(calls), 2)
        self.assertEqual(controller.state()["status"], "stopped")
        self.assertFalse(controller.credentials_path.exists())

    def test_invalid_pools_and_stage_requests_stop_before_access(self):
        for environment in ({"CLIPFORGE_COLAB_POOL": "A100"}, {"CLIP_INDEX": "0"}):
            with (
                patch.dict(
                    os.environ,
                    {
                        **REQUEST,
                        "COLAB_ADC_JSON": "{}",
                        "AUDIO_STAGE": "render",
                        "PREVIOUS_RUN": "1",
                        "PREVIOUS_REQUEST": "c" * 32,
                        "REVIEW_FILE": "/nonexistent",
                        **environment,
                    },
                ),
                patch.object(control, "authenticated_client") as authenticated,
                tempfile.TemporaryDirectory() as temporary,
            ):
                with self.assertRaises((control.ControlError, OSError)):
                    control.Controller(temporary).start("audio-stage")
                authenticated.assert_not_called()


class AllocationWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.controller = control.Controller(temporary)
        self.elapsed = 0

        def sleep(seconds):
            self.elapsed += seconds

        self.sleep = self.enterContext(
            patch.object(control.time, "sleep", side_effect=sleep)
        )
        self.enterContext(
            patch.object(control.time, "monotonic", side_effect=lambda: self.elapsed)
        )
        self.client = Mock()
        self.client.list_assignments.return_value = []
        self.client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        self.enterContext(
            patch.object(control, "authenticated_client", return_value=self.client)
        )
        self.enterContext(patch.object(control, "stage_request", return_value={}))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    **REQUEST,
                    "COLAB_ADC_JSON": "{}",
                    "CLIPFORGE_COLAB_POOL": "",
                    "CLIPFORGE_COLAB_SHAPE": "high-ram",
                },
            )
        )
        os.environ.pop("CLIPFORGE_ALLOCATION_WAIT_SECONDS", None)
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_all_refusals_wait_then_restart_each_mode_pool_from_the_first_candidate(
        self,
    ):
        from colab_cli.client import TooManyAssignmentsError

        # Waiting is opt-in; exercise the production setting instead of relying
        # on the zero-wait default used by other control operations.
        os.environ["CLIPFORGE_ALLOCATION_WAIT_SECONDS"] = "600"
        for mode in ("asr", "audio-stage"):
            with self.subTest(mode=mode):
                pool = control.colab_pool(mode)
                self.client.assign.side_effect = [
                    *[TooManyAssignmentsError() for _ in pool],
                    granted_assignment(),
                ]
                self.controller.start(mode)
                attempts = self.client.assign.call_args_list
                self.assertEqual(
                    [
                        (call.kwargs["accelerator"], call.kwargs["shape"])
                        for call in attempts
                    ],
                    pool + [pool[0]],
                )
                self.assertEqual(self.controller.state()["status"], "allocated")
                self.sleep.assert_called_once_with(60)
                self.client.list_assignments.assert_called_once_with()
                self.client.get_consumption_user_info.assert_called_once_with()
                lines = self.stdout.getvalue().splitlines()
                wait_lines = [
                    line for line in lines if line.startswith("割り当て待ち:")
                ]
                kinds = ", ".join(f"{acc.value}/{shape.name}" for acc, shape in pool)
                self.assertEqual(
                    wait_lines,
                    [f"割り当て待ち: 1回目、経過0秒、拒否された種類: {kinds}"],
                )
                self.assertEqual(sum("before allocation" in line for line in lines), 1)
                control.write_private(self.controller.state_path, {"status": "stopped"})
                self.client.reset_mock()
                self.sleep.reset_mock()
                self.stdout.seek(0)
                self.stdout.truncate()
                self.elapsed = 0

    def test_wait_limit_stops_record_and_removes_credentials(self):
        from colab_cli.client import TooManyAssignmentsError

        os.environ["CLIPFORGE_ALLOCATION_WAIT_SECONDS"] = "125"
        self.client.assign.side_effect = TooManyAssignmentsError()
        with self.assertRaisesRegex(control.ControlError, "refused every runtime"):
            self.controller.start()
        self.assertEqual(
            [call.args[0] for call in self.sleep.call_args_list], [60, 60, 5]
        )
        self.assertEqual(self.elapsed, 125)
        self.assertEqual(self.client.assign.call_count, 12)
        state = self.controller.state()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(
            state["allocation_refused"], ["T4/HIGH_RAM", "T4/STANDARD", "L4/STANDARD"]
        )
        self.assertFalse(self.controller.credentials_path.exists())
        self.client._get_assignment.assert_not_called()
        self.client.list_assignments.assert_called_once_with()
        self.client.get_consumption_user_info.assert_called_once_with()
        wait_lines = [
            line
            for line in self.stdout.getvalue().splitlines()
            if line.startswith("割り当て待ち:")
        ]
        self.assertEqual(len(wait_lines), 3)
        self.assertIn("2回目、経過60秒", wait_lines[1])
        self.assertIn("3回目、経過120秒", wait_lines[2])

    def test_non_refusal_reconciles_and_raises_without_allocation_wait(self):
        error = RuntimeError("synthetic allocation failure")
        self.client.assign.side_effect = error
        with patch.object(self.controller, "reconcile_assignment") as reconcile:
            with self.assertRaises(RuntimeError) as raised:
                self.controller.start()
            self.assertIs(raised.exception, error)
            reconcile.assert_called_once_with(
                self.client,
                {**self.controller.state(), "allocation_error": "RuntimeError"},
            )
        self.client.assign.assert_called_once()
        self.sleep.assert_not_called()
        self.assertNotIn("割り当て待ち:", self.stdout.getvalue())


class RenderedChecksumTests(unittest.TestCase):
    CLIP = "delivery/clips/clip-001"
    FILES = {
        "video.mp4": b"video",
        "captions.ass": b"ass",
        "captions.srt": b"srt",
        "ledger.json": b"{}",
        "quality.json": b"{}",
    }

    @staticmethod
    def _sha(files):
        return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}

    def _collect(self, temporary, files, sha256, clip_index=None, directories=None):
        output = Path(temporary) / "output"
        output.mkdir(parents=True)
        directories = [self.CLIP] if directories is None else directories
        for name in directories:
            directory = output / name
            directory.mkdir(parents=True)
            for name, data in files.items():
                (directory / name).write_bytes(data)
        status = "clip_rendered" if clip_index else "complete"
        (output / "request-receipt.json").write_text(
            json.dumps(
                {
                    "request_id": "a" * 32,
                    "source_ref": "b" * 40,
                    "status": status,
                    "sha256": {},
                }
            )
        )
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "rendered": [
                        {"directory": name, "sha256": sha256} for name in directories
                    ],
                }
            )
        )
        record = {
            "mode": "audio-stage",
            "request_id": "a" * 32,
            "source_ref": "b" * 40,
            "input_tag": "clipforge-input-" + "a" * 32,
            "stage": {"clip_index": clip_index},
        }
        controller = control.Controller(Path(temporary) / "state")
        with patch.object(control, "ReleaseBundles") as bundles:
            bundles.return_value.restore.return_value = True
            with contextlib.redirect_stdout(io.StringIO()):
                controller.collect(record, output, False, "test-only")

    def test_extra_ledger_and_quality_checksums_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._collect(temporary, self.FILES, self._sha(self.FILES))

    def test_single_requested_clip_accepts_both_directory_prefixes(self):
        for directory in ("clips/clip-001", "delivery/clips/clip-001"):
            with (
                self.subTest(directory=directory),
                tempfile.TemporaryDirectory() as temporary,
            ):
                self._collect(
                    temporary,
                    self.FILES,
                    self._sha(self.FILES),
                    clip_index="1",
                    directories=[directory],
                )

    def test_single_requested_clip_rejects_mismatched_missing_or_multiple_results(self):
        for directories in (
            ["delivery/clips/clip-002"],
            [],
            ["delivery/clips/clip-001", "delivery/clips/clip-002"],
        ):
            with (
                self.subTest(directories=directories),
                tempfile.TemporaryDirectory() as temporary,
            ):
                with self.assertRaisesRegex(control.ControlError, "clip_index"):
                    self._collect(
                        temporary,
                        self.FILES,
                        self._sha(self.FILES),
                        clip_index="1",
                        directories=directories,
                    )

    def test_missing_required_file_checksum_is_rejected(self):
        files = dict(self.FILES)
        sha256 = self._sha(files)
        del sha256["captions.srt"]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                control.ControlError, "Missing result checksums"
            ):
                self._collect(temporary, files, sha256)

    def test_parent_directory_name_is_rejected(self):
        sha256 = self._sha(self.FILES)
        sha256["../escape.json"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                control.ControlError, "Unexpected result file name"
            ):
                self._collect(temporary, self.FILES, sha256)

    def test_slash_in_name_is_rejected(self):
        sha256 = self._sha(self.FILES)
        sha256["sub/extra.json"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                control.ControlError, "Unexpected result file name"
            ):
                self._collect(temporary, self.FILES, sha256)

    def test_digest_mismatch_on_extra_file_is_rejected(self):
        sha256 = self._sha(self.FILES)
        sha256["ledger.json"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(control.ControlError, "checksum mismatch"):
                self._collect(temporary, self.FILES, sha256)

    def test_listed_file_missing_on_disk_is_rejected(self):
        files = {
            name: data for name, data in self.FILES.items() if name != "quality.json"
        }
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(control.ControlError, "Missing result file"):
                self._collect(temporary, files, self._sha(self.FILES))


class StageConfigFileTests(unittest.TestCase):
    def stage_environment(self, review, **extra):
        return {
            "AUDIO_STAGE": "render",
            "PREVIOUS_RUN": "123",
            "PREVIOUS_REQUEST": "c" * 32,
            "REVIEW_FILE": str(review),
            **extra,
        }

    def test_stage_request_carries_a_config_name_only_when_given(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary) / "review.json"
            review.write_text('{"clips": []}')
            with patch.dict(os.environ, self.stage_environment(review)):
                os.environ.pop("CONFIG_FILE", None)
                self.assertIsNone(control.stage_request()["config_file"])
            with patch.dict(
                os.environ,
                self.stage_environment(
                    review, CONFIG_FILE="config.landscape-chat.example.json"
                ),
            ):
                stage = control.stage_request()
            self.assertEqual(stage["config_file"], "config.landscape-chat.example.json")

    def test_stage_request_rejects_names_outside_the_pattern(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary) / "review.json"
            review.write_text('{"clips": []}')
            for name in (
                "../config.landscape.example.json",
                "config.Chat.example.json",
            ):
                with (
                    self.subTest(name=name),
                    patch.dict(
                        os.environ, self.stage_environment(review, CONFIG_FILE=name)
                    ),
                ):
                    with self.assertRaises(control.ControlError):
                        control.stage_request()
