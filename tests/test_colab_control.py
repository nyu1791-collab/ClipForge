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
    "AUDIO_URL": "",
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
    def test_audio_url_is_recorded_without_changing_empty_url_requests(self):
        for url in (AUDIO_URL, ""):
            with (
                self.subTest(url=url),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    os.environ,
                    {**REQUEST, "AUDIO_URL": url, "COLAB_ADC_JSON": "{}"},
                ),
                patch.object(control, "authenticated_client") as authenticate,
            ):
                client = authenticate.return_value
                client.list_assignments.return_value = []
                client.get_consumption_user_info.side_effect = RuntimeError()
                client.assign.return_value = SimpleNamespace(
                    endpoint="owned-session",
                    runtime_proxy_info=SimpleNamespace(
                        url="https://test.invalid", token="test-only"
                    ),
                )
                controller = control.Controller(temporary)
                controller.start(mode="pipeline")
                if url:
                    self.assertEqual(controller.state()["audio_url"], url)
                else:
                    self.assertNotIn("audio_url", controller.state())

    def test_invalid_audio_urls_stop_before_authentication(self):
        for url in (
            AUDIO_URL.replace("https:", "http:"),
            AUDIO_URL.replace("kick.com/", "kick.com.evil.invalid/"),
            AUDIO_URL.replace("kick.com/", "user@kick.com/"),
            AUDIO_URL.replace("kick.com/", "kick.com:443/"),
            AUDIO_URL.replace("test_channel-1", ".."),
            AUDIO_URL.replace("01234567-89ab", "not-uuid"),
            AUDIO_URL + "/",
            AUDIO_URL + "?query=1",
            AUDIO_URL + "#fragment",
            AUDIO_URL + "\n",
            " " + AUDIO_URL,
            " ",
        ):
            with (
                self.subTest(url=url),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    os.environ,
                    {**REQUEST, "AUDIO_URL": url, "COLAB_ADC_JSON": "invalid-json"},
                ),
                patch.object(control, "authenticated_client") as authenticate,
                patch.object(control, "colab_pool") as pool,
            ):
                controller = control.Controller(temporary)
                with self.assertRaisesRegex(control.ControlError, "Kick audio URL"):
                    controller.start(mode="pipeline")
                authenticate.assert_not_called()
                pool.assert_not_called()
                self.assertFalse(controller.state_path.exists())
                self.assertFalse(controller.credentials_path.exists())

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
            self.assertIn("Compute units used by this job: 0.40", lines)
        client.get_consumption_user_info.side_effect = RuntimeError("not reported")
        self.assertIsNone(control.compute_units(client))

    def test_high_ram_shape_is_requested_and_unknown_shapes_stop_before_access(self):
        from colab_cli.client import Shape

        for name, expected in (("high-ram", Shape.HIGH_RAM), ("", Shape.STANDARD)):
            with (
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    os.environ,
                    {
                        **REQUEST,
                        "COLAB_ADC_JSON": '{"type":"authorized_user"}',
                        "CLIPFORGE_COLAB_SHAPE": name,
                    },
                ),
            ):
                client = Mock()
                client.list_assignments.return_value = []
                client.assign.return_value = SimpleNamespace(
                    endpoint="owned-session",
                    runtime_proxy_info=SimpleNamespace(
                        url="https://t.invalid", token="t"
                    ),
                )
                with patch.object(control, "authenticated_client", return_value=client):
                    controller = control.Controller(temporary)
                    controller.start()
                self.assertEqual(client.assign.call_args.kwargs["shape"], expected)
                self.assertEqual(controller.state()["shape"], expected.name)
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    **REQUEST,
                    "COLAB_ADC_JSON": '{"type":"authorized_user"}',
                    "CLIPFORGE_COLAB_SHAPE": "huge",
                },
            ),
            patch.object(control, "authenticated_client") as authenticated,
        ):
            controller = control.Controller(temporary)
            with self.assertRaisesRegex(control.ControlError, "Unknown Colab shape"):
                controller.start()
            authenticated.assert_not_called()
            self.assertFalse(controller.credentials_path.exists())

    def test_explicit_allocation_refusal_does_not_retry_or_leave_credentials(self):
        from colab_cli.client import TooManyAssignmentsError

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ, {**REQUEST, "COLAB_ADC_JSON": '{"type":"authorized_user"}'}
            ),
        ):
            client = Mock()
            client.list_assignments.return_value = []
            client.assign.side_effect = TooManyAssignmentsError()
            with patch.object(control, "authenticated_client", return_value=client):
                controller = control.Controller(temporary)
                with self.assertRaisesRegex(control.ControlError, "refused"):
                    controller.start()
                self.assertEqual(client.assign.call_count, 1)
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

    def test_failed_gpu_check_keeps_private_report_and_closes_connection(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"GH_TOKEN": "test-only"}),
            patch("colab_cli.contents.ContentsClient") as contents_type,
            patch.object(control, "runtime_from_state") as runtime_type,
            patch.object(control, "ReleaseBundles"),
            patch.object(
                control, "checked_execute", side_effect=RuntimeError("failed")
            ),
        ):
            controller = control.Controller(Path(temporary) / "state")
            record = {
                "status": "allocated",
                "mode": "verify-gpu",
                "source_ref": "b" * 40,
                "endpoint": "owned-session",
                "url": "https://test.invalid",
                "token": "test-only",
            }
            control.write_private(controller.state_path, record)
            report = {
                "mode": "verify-gpu",
                "source_ref": "b" * 40,
                "status": "failed",
                "stage": "cuda-asr-verification",
            }

            def download(remote, local):
                Path(local).write_text(json.dumps(report))

            contents_type.return_value.download.side_effect = download
            client = Mock()
            client.list_assignments.return_value = [
                SimpleNamespace(
                    endpoint="owned-session",
                    runtime_proxy_info=SimpleNamespace(
                        url=record["url"], token="refreshed-test-token"
                    ),
                )
            ]
            output = Path(temporary) / "output"
            with patch.object(controller, "client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    controller.run(output)
            saved = output / "colab-report.json"
            self.assertEqual(json.loads(saved.read_text()), report)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            runtime_type.return_value.stop.assert_called_once_with()
            self.assertFalse((controller.root / "job-payload.json").exists())

    def _pipeline_controller(self, temporary, restore, execute=None):
        controller = control.Controller(Path(temporary) / "state")
        control.write_private(
            controller.state_path,
            {
                "status": "allocated",
                "mode": "pipeline",
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
        def restore(tag, destination, kind, identity, required=False):
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
                self._result_restore("complete", "complete", timings={"pipeline": 1.5}),
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

    def test_audio_url_payload_needs_no_gemini_key_and_collects_topics(self):
        for key in ("", "test-only"):
            with (
                self.subTest(key_present=bool(key)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                restore_complete = self._result_restore("topics_allocated", "complete")

                def restore(*args, **kwargs):
                    restore_complete(*args, **kwargs)
                    receipt_path = Path(args[1]) / "request-receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    receipt["audio_source_url"] = AUDIO_URL
                    receipt_path.write_text(json.dumps(receipt))

                controller, events, patches = self._pipeline_controller(
                    temporary, restore
                )
                record = controller.state()
                record["audio_url"] = AUDIO_URL
                control.write_private(controller.state_path, record)
                uploaded = []

                def upload(local, remote):
                    uploaded.append(json.loads(Path(local).read_text()))

                with contextlib.ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    stack.enter_context(patch.dict(os.environ, {"GEMINI_API_KEY": key}))
                    contents = stack.enter_context(
                        patch("colab_cli.contents.ContentsClient")
                    )
                    contents.return_value.upload.side_effect = upload
                    controller.run(Path(temporary) / "output")
                self.assertEqual(uploaded[0]["audio_url"], AUDIO_URL)
                self.assertEqual(uploaded[0]["gemini_api_key"], "")
                self.assertEqual(events, ["stop", "restore"])

    def test_stage_timings_are_printed_from_the_returned_receipt(self):
        timings = {"cache_restore": 0.25, "setup": 12.5, "pipeline": 300.75}
        with tempfile.TemporaryDirectory() as temporary:
            controller, events, patches = self._pipeline_controller(
                temporary,
                self._result_restore("complete", "complete", timings=timings),
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

    def test_report_stages_prints_the_worker_error_on_its_own_line(self):
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
            "Worker error: PipelineError: " + message,
        )

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
                self._result_restore("complete", "complete"),
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
        if isinstance(lookup, Exception):
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
        controller, _ = self.start_with_failed_assign(unassigned_response())
        self.assertEqual(controller.state()["status"], "stopped")
        self.assertNotIn("token", controller.state())
        self.assertFalse(controller.credentials_path.exists())

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

    def test_clip_rendering_tries_the_cheapest_runtime_first(self):
        controller, calls, Accelerator = self.start_stage(refusals=0)
        self.assertEqual(calls, [(Accelerator.NONE, "HIGH_RAM")])
        state = controller.state()
        self.assertEqual((state["accelerator"], state["shape"]), ("NONE", "HIGH_RAM"))
        self.assertEqual(state["stage"]["clip_index"], "2")

    def test_a_412_refusal_moves_to_the_next_runtime_in_the_pool(self):
        controller, calls, Accelerator = self.start_stage(refusals=2)
        self.assertEqual(
            calls,
            [
                (Accelerator.NONE, "HIGH_RAM"),
                (Accelerator.NONE, "STANDARD"),
                (Accelerator.T4, "HIGH_RAM"),
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
                controller.collect(record, output, False, "test-only", None)

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
