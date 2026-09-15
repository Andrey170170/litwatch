"""Primary routing preferences must not leak into the other cascade models."""

from unittest.mock import Mock

import pytest

from litwatch import api, config
from litwatch.config import Settings


@pytest.mark.parametrize(
    ("routing", "expected"),
    [
        (None, ("openai/flex", "openai")),
        ("", ()),
        (" openai , openai/flex ", ("openai", "openai/flex")),
    ],
)
def test_provider_order_environment(monkeypatch, routing, expected):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("LITWATCH_SCREEN_PROVIDER_ORDER", raising=False)
    monkeypatch.delenv("LITWATCH_SCREEN_TIMEOUT_SECONDS", raising=False)
    if routing is not None:
        monkeypatch.setenv("LITWATCH_SCREEN_PROVIDER_ORDER", routing)
    settings = Settings.from_env()
    assert settings.screen_provider_order == expected
    assert settings.screen_timeout_seconds == 120.0
    monkeypatch.setenv("LITWATCH_SCREEN_TIMEOUT_SECONDS", "45.5")
    assert Settings.from_env().screen_timeout_seconds == 45.5


@pytest.mark.parametrize(
    ("model", "routing", "expected"),
    [
        ("openai/gpt-5.6-luna", ("openai/flex", "openai"), ("openai/flex", "openai")),
        ("openai/gpt-5.6-luna", (), ()),
        ("another/model", ("openai/flex", "openai"), ()),
    ],
)
def test_cascade_routes_only_luna_primary(monkeypatch, model, routing, expected):
    client = object()
    monkeypatch.setattr(api, "openrouter_client", lambda key: client)
    monkeypatch.setattr(api, "Cascade", Mock())
    screener = Mock()
    monkeypatch.setattr(api, "ModelScreener", screener)
    settings = Settings(
        openrouter_api_key="test",
        screen_model=model,
        screen_provider_order=routing,
        screen_timeout_seconds=75.0,
    )
    api.build_cascade(settings, [])
    primary, recall, confirmation = screener.call_args_list
    assert primary.args == (client, model, settings.screen_effort)
    assert primary.kwargs == {"provider_order": expected, "timeout_seconds": 75.0}
    assert recall.kwargs == {
        "max_completion_tokens": settings.rescreen_max_completion_tokens,
        "operation": "rescreen",
    }
    assert confirmation.kwargs == {"operation": "confirm"}
