import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_actions as control

REQUEST = {
    "REQUEST_ID": "a" * 32,
    "CLIPFORGE_SOURCE_REF": "b" * 40,
    "INPUT_TAG": "clipforge-input-" + "a" * 32,
}


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
                with self.assertRaisesRegex(control.ControlError, "report was saved"):
                    controller.run(output)
            saved = output / "colab-report.json"
            self.assertEqual(json.loads(saved.read_text()), report)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            runtime_type.return_value.stop.assert_called_once_with()
            self.assertFalse((controller.root / "job-payload.json").exists())


if __name__ == "__main__":
    unittest.main()
