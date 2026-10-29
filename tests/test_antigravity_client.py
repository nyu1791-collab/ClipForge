import base64

import pytest

from clipforge import antigravity_client as client


class _Response:
    async def text(self):
        return '{"ok": true}'


class _Agent:
    configs = []

    def __init__(self, config):
        _Agent.configs.append(config)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, content):
        self.content = content
        return _Response()


def test_text_and_audio_parts_become_sdk_content():
    audio = base64.b64encode(b"ogg-bytes").decode()
    content = client._content(
        [{"text": "prompt"}, {"inlineData": {"mimeType": "audio/ogg", "data": audio}}]
    )
    assert content[0] == "prompt"
    assert content[1].data == b"ogg-bytes"
    assert content[1].mime_type == "audio/ogg"


def test_unknown_part_is_not_retried():
    with pytest.raises(client.AntigravityError) as error:
        client._content([{"functionCall": {"name": "x"}}])
    assert error.value.retryable is False


def test_unrouted_model_is_rejected_before_any_call():
    with pytest.raises(client.AntigravityError) as error:
        client.generate_text("gemini-2.5-pro", "MEDIUM", "", [{"text": "x"}])
    assert error.value.retryable is False


def test_request_carries_stage_thinking_rules_and_no_tools(monkeypatch):
    monkeypatch.setattr(client, "Agent", _Agent)
    text = client.generate_text("gemini-3.8-flash", "MEDIUM", "RULES", [{"text": "p"}])
    assert text == '{"ok": true}'
    config = _Agent.configs[-1]
    model = next(m for m in config.models if m.name == "gemini-3.8-flash")
    assert model.endpoint.options.thinking_level == client.ThinkingLevel.MEDIUM
    assert config.system_instructions == "RULES"
    assert config.capabilities.enabled_tools == []


def test_research_request_enables_only_web_search(monkeypatch):
    monkeypatch.setattr(client, "Agent", _Agent)
    client.generate_text(
        "gemini-3.5-flash", "MINIMAL", "", [{"text": "q"}], research=True
    )
    config = _Agent.configs[-1]
    assert config.capabilities.enabled_tools == [client.types.BuiltinTools.SEARCH_WEB]


@pytest.mark.parametrize(
    "message, retryable",
    [
        ("429 RESOURCE_EXHAUSTED: quota", True),
        ("503 UNAVAILABLE: try later", True),
        ("PERMISSION_DENIED: the caller lacks access", False),
    ],
)
def test_failures_are_classified_and_their_text_is_not_kept(
    monkeypatch, message, retryable
):
    async def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(client, "_chat", fail)
    with pytest.raises(client.AntigravityError) as error:
        client.generate_text("gemini-3.8-flash", "MEDIUM", "", [{"text": "x"}])
    assert error.value.retryable is retryable
    assert message not in str(error.value)


def test_empty_response_is_retryable(monkeypatch):
    async def empty(*args, **kwargs):
        return "  "

    monkeypatch.setattr(client, "_chat", empty)
    with pytest.raises(client.AntigravityError) as error:
        client.generate_text("gemini-3.8-flash", "MEDIUM", "", [{"text": "x"}])
    assert error.value.retryable is True
