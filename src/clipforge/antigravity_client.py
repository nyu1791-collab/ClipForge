"""Gemini 3.5 / 3.8 Flash text and audio calls through the Antigravity SDK on Vertex.

Gemini 3.5 Transcribe is not routed here: transcribe_pipeline keeps its direct Vertex
request. Failure messages carry only the exception type, never the request or response
text, so audio, transcripts and credentials stay out of logs.
"""

import asyncio
import base64
import tempfile

from google.antigravity import Agent, LocalAgentConfig, types
from google.antigravity.models import (
    GeminiModelOptions,
    ModelTarget,
    ThinkingLevel,
    VertexEndpoint,
)

from .gemini_audio_editor import PROJECT

LOCATION = "global"
ANTIGRAVITY_MODELS = frozenset({"gemini-3.5-flash", "gemini-3.8-flash"})
TIMEOUT_SECONDS = 1800
# The same classes the Vertex path retried: 429 and 5xx, plus a timeout.
TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
)


class AntigravityError(Exception):
    def __init__(self, message, retryable):
        super().__init__(message)
        self.retryable = retryable


def _content(parts):
    """Convert the Vertex-shaped parts used by the caller into SDK content."""
    content = []
    for part in parts:
        if "text" in part:
            content.append(part["text"])
        elif "inlineData" in part:
            payload = part["inlineData"]
            content.append(
                types.Audio(
                    data=base64.b64decode(payload["data"], validate=True),
                    mime_type=payload["mimeType"],
                )
            )
        else:
            raise AntigravityError("Unsupported Gemini part", retryable=False)
    return content


async def _chat(model, thinking, system_text, content, research, max_tokens):
    endpoint = VertexEndpoint(
        project=PROJECT,
        location=LOCATION,
        options=GeminiModelOptions(
            thinking_level=ThinkingLevel[thinking],
            max_output_tokens=max_tokens,
        ),
    )
    # vertex=True selects the Vertex backend; without it the SDK validates the
    # per-model endpoint as a Gemini API endpoint and demands an API key.
    config = LocalAgentConfig(
        vertex=True,
        project=PROJECT,
        location=LOCATION,
        models=[ModelTarget(name=model, endpoint=endpoint)],
        system_instructions=system_text or None,
        workspaces=[tempfile.mkdtemp(prefix="clipforge-antigravity-")],
        capabilities=types.CapabilitiesConfig(
            enabled_tools=[types.BuiltinTools.SEARCH_WEB] if research else [],
            enable_subagents=False,
            agent_behavior=types.AgentBehavior.MINIMAL,
        ),
    )
    async with Agent(config) as agent:
        response = await agent.chat(content)
        return await response.text()


def generate_text(
    model, thinking, system_text, parts, research=False, max_tokens=65536
):
    """Return the model's text for one request. Raises AntigravityError on failure."""
    if model not in ANTIGRAVITY_MODELS:
        raise AntigravityError(
            "Model is not routed through Antigravity", retryable=False
        )
    content = _content(parts)
    try:
        text = asyncio.run(
            asyncio.wait_for(
                _chat(model, thinking, system_text, content, research, max_tokens),
                TIMEOUT_SECONDS,
            )
        )
    except asyncio.TimeoutError:
        raise AntigravityError(
            "Antigravity request timed out", retryable=True
        ) from None
    except Exception as error:
        message = str(error)
        retryable = any(marker in message for marker in TRANSIENT_MARKERS)
        raise AntigravityError(
            "Antigravity request failed: " + type(error).__name__, retryable=retryable
        ) from None
    if not text or not text.strip():
        raise AntigravityError("Antigravity returned an empty response", retryable=True)
    return text
