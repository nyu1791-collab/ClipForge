"""Offline checks with installed browser-use/Google SDK; no browser is launched."""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import research


class InstalledResearchChecks(unittest.TestCase):
    def check_transport(self, status_code):
        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "ANONYMIZED_TELEMETRY": "false",
                        "BROWSER_USE_CLOUD_SYNC": "false",
                        "BROWSER_USE_SETUP_LOGGING": "false",
                        "PYTHON_DOTENV_DISABLED": "1",
                        "BROWSER_USE_CONFIG_DIR": str(directory / "config"),
                    },
                )
            )
            for name in (
                "socket.socket.connect",
                "socket.socket.connect_ex",
                "socket.getaddrinfo",
            ):
                stack.enter_context(
                    patch(name, side_effect=AssertionError("External network disabled"))
                )

            import httpx
            from browser_use import Agent, Browser, ChatGoogle
            from browser_use.llm.exceptions import ModelProviderError
            from browser_use.llm.messages import UserMessage
            from google.auth.credentials import AnonymousCredentials

            original_chat_init = ChatGoogle.__init__
            original_agent_init = Agent.__init__
            requests = []

            def handle(request):
                requests.append(request)
                body = (
                    {
                        "error": {
                            "code": 503,
                            "message": "offline fixture",
                            "status": "UNAVAILABLE",
                        }
                    }
                    if status_code == 503
                    else {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "offline transport response"}],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                )
                return httpx.Response(status_code, json=body)

            def initialize_chat(instance, **options):
                credentials = AnonymousCredentials()
                credentials.token = "offline-transport-test-token"
                options["credentials"] = credentials
                options["http_options"]["async_client_args"] = {
                    "transport": httpx.MockTransport(handle)
                }
                original_chat_init(instance, **options)

            def initialize_agent(instance, **options):
                options["file_system_path"] = str(directory / "agent-files")
                original_agent_init(instance, **options)

            async def exercise(instance, *, max_steps, on_step_start):
                self.assertEqual(max_steps, 5)
                self.assertEqual(instance.settings.max_failures, 1)
                self.assertFalse(instance.settings.use_judge)
                self.assertFalse(instance.settings.final_response_after_failure)
                self.assertFalse(instance.settings.message_compaction.enabled)
                actions = set(instance.tools.registry.registry.actions)
                self.assertLessEqual(actions, research.RESEARCH_ACTIONS)
                self.assertIn("navigate", actions)
                self.assertIn("done", actions)
                self.assertFalse(instance.browser_profile.enable_default_extensions)
                self.assertFalse(instance.browser_profile.captcha_solver)
                await on_step_start(instance)
                try:
                    reply = await instance.llm.ainvoke([UserMessage(content="offline")])
                    self.assertEqual(reply.completion, "offline transport response")
                    with self.assertRaisesRegex(RuntimeError, "要求上限"):
                        await instance.llm.ainvoke([UserMessage(content="retry")])
                finally:
                    client = instance.llm.get_client()
                    await client.aio.aclose()
                    client.close()
                return SimpleNamespace(
                    final_result=lambda: "offline",
                    is_successful=lambda: True,
                    is_done=lambda: True,
                    has_errors=lambda: False,
                )

            stack.enter_context(patch.object(ChatGoogle, "__init__", initialize_chat))
            stack.enter_context(patch.object(Agent, "__init__", initialize_agent))
            stack.enter_context(patch.object(Agent, "run", exercise))
            closed = stack.enter_context(
                patch.object(Browser, "kill", new_callable=AsyncMock)
            )
            output = directory / "result.json"

            def invoke():
                return asyncio.run(
                    research.browse(
                        "https://example.test",
                        "offline check",
                        "offline-fixture-model",
                        "vertex",
                        "offline-project",
                        output,
                    )
                )

            if status_code == 503:
                with self.assertRaises(ModelProviderError):
                    invoke()
            else:
                invoke()
                record = json.loads(output.read_text(encoding="utf-8"))
                self.assertTrue(record["completed"])
                self.assertEqual(record["model_request_attempts"], 1)
            self.assertEqual(len(requests), 1)
            closed.assert_awaited_once()

    def test_real_agent_registry_sdk_and_request_guard(self):
        self.check_transport(200)

    def test_transient_sdk_failure_has_one_transport_attempt(self):
        self.check_transport(503)


if __name__ == "__main__":
    unittest.main()
