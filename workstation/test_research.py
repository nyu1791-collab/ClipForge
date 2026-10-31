"""Exercise the optional browser adapter without a browser or paid requests."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import research


def fake_browser_use(*, steps=1, repeat=False, init_error=False, api_error=False):
    state = {"calls": 0, "killed": False}

    class Browser:
        def __init__(self, **options):
            state["browser_options"] = options

        async def kill(self):
            state["killed"] = True

    class Tools:
        def __init__(self):
            names = research.RESEARCH_ACTIONS | {
                "click",
                "input",
                "evaluate",
                "extract",
                "upload_file",
                "send_keys",
                "write_file",
                "read_file",
                "select_dropdown",
                "new_upstream_action",
            }
            self.registry = SimpleNamespace(
                registry=SimpleNamespace(actions=dict.fromkeys(names))
            )
            state["tools"] = self

        def exclude_action(self, name):
            self.registry.registry.actions.pop(name, None)

    class ChatGoogle:
        def __init__(self, **options):
            # The pinned upstream requires at least one total attempt.
            if options["max_retries"] < 1:
                raise ValueError("max_retries must be at least 1")
            state["model_options"] = options

        async def ainvoke(self, *args, **kwargs):
            state["calls"] += 1
            if api_error:
                raise ConnectionError("synthetic provider failure")
            return "synthetic model reply"

    class Agent:
        def __init__(self, **options):
            if init_error:
                raise RuntimeError("synthetic initialization failure")
            state["agent_options"] = options
            self.llm = options["llm"]
            # Check restrictions again after Agent modifies its tools.
            options["tools"].registry.registry.actions["evaluate"] = None

        async def run(self, *, max_steps, on_step_start):
            state["max_steps"] = max_steps
            for _ in range(steps):
                await on_step_start(self)
                await self.llm.ainvoke(["synthetic input"])
                if repeat:
                    await self.llm.ainvoke(["implicit retry with clarification"])
            return SimpleNamespace(
                final_result=lambda: "not completed",
                is_done=lambda: True,
                is_successful=lambda: False,
                has_errors=lambda: False,
            )

    return SimpleNamespace(
        Agent=Agent, Browser=Browser, Tools=Tools, ChatGoogle=ChatGoogle
    ), state


class BrowserResearchTests(unittest.TestCase):
    def invoke(self, fake, output):
        with patch.dict(sys.modules, {"browser_use": fake}):
            return asyncio.run(
                research.browse(
                    "https://example.test/page",
                    "公開資料を調べる",
                    "exact-model-id",
                    "vertex",
                    "explicit-project",
                    output,
                )
            )

    def test_explicit_route_no_judge_and_failed_done_is_not_success(self):
        fake, state = fake_browser_use()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            self.invoke(fake, output)
            record = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(record["completed"])
        self.assertTrue(record["finished"])
        self.assertEqual(record["model_request_attempts"], 1)
        self.assertTrue(state["killed"])
        self.assertEqual(
            state["model_options"],
            {
                "model": "exact-model-id",
                "max_retries": 1,
                "http_options": {"retry_options": {"attempts": 1}},
                "vertexai": True,
                "project": "explicit-project",
                "location": "global",
            },
        )
        options = state["agent_options"]
        self.assertEqual(options["max_failures"], 1)
        self.assertFalse(options["final_response_after_failure"])
        self.assertFalse(options["use_judge"])
        self.assertFalse(options["message_compaction"])
        self.assertIsNone(options["fallback_llm"])
        self.assertEqual(state["browser_options"]["allowed_domains"], ["example.test"])
        self.assertFalse(state["browser_options"]["enable_default_extensions"])
        self.assertFalse(state["browser_options"]["captcha_solver"])
        actions = state["tools"].registry.registry.actions
        self.assertEqual(set(actions), research.RESEARCH_ACTIONS)

    def test_implicit_retry_is_blocked_before_a_second_provider_call(self):
        fake, state = fake_browser_use(repeat=True)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            with self.assertRaisesRegex(RuntimeError, "要求上限"):
                self.invoke(fake, output)
            self.assertFalse(output.exists())
        self.assertEqual(state["calls"], 1)
        self.assertTrue(state["killed"])

    def test_total_limit_is_enforced_even_if_upstream_exceeds_step_limit(self):
        fake, state = fake_browser_use(steps=6)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "要求上限"):
                self.invoke(fake, Path(temporary) / "result.json")
        self.assertEqual(state["calls"], 5)
        self.assertTrue(state["killed"])

    def test_initialization_failure_still_closes_browser(self):
        fake, state = fake_browser_use(init_error=True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "initialization"):
                self.invoke(fake, Path(temporary) / "result.json")
        self.assertEqual(state["calls"], 0)
        self.assertTrue(state["killed"])

    def test_provider_failure_does_not_repeat_a_paid_request(self):
        fake, state = fake_browser_use(api_error=True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConnectionError, "provider failure"):
                self.invoke(fake, Path(temporary) / "result.json")
        self.assertEqual(state["calls"], 1)
        self.assertTrue(state["killed"])


if __name__ == "__main__":
    unittest.main()
