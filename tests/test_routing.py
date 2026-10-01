"""Every OpenRouter request names its providers; lists belong to models, not stages."""

from unittest.mock import Mock

import pytest

from litwatch import api, config
from litwatch.config import Settings
from litwatch.routing import DEFAULT_PROVIDERS, parse_providers, provider_preferences


def test_preferences_restrict_listed_models_and_always_require_parameters():
    assert provider_preferences(("openai/flex", "openai")) == {
        "require_parameters": True,
        "order": ["openai/flex", "openai"],
        "only": ["openai/flex", "openai"],
    }
    assert provider_preferences(()) == {"require_parameters": True}


def test_environment_overrides_merge_over_defaults(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("LITWATCH_PROVIDERS", " z-ai/glm-5.3 = baidu | reka ; openai/gpt-5.6-luna=")
    settings = Settings.from_env()
    assert settings.providers_for("z-ai/glm-5.3") == ("baidu", "reka")
    assert settings.providers_for("openai/gpt-5.6-luna") == ()
    assert settings.providers_for("meta/muse-spark-1.3") == DEFAULT_PROVIDERS["meta/muse-spark-1.3"]
    assert settings.providers_for("unlisted/model") == ()
    assert parse_providers("", DEFAULT_PROVIDERS) == DEFAULT_PROVIDERS


@pytest.mark.parametrize("model", ["openai/gpt-5.6-luna", "another/model"])
def test_each_stage_gets_its_own_models_providers(monkeypatch, model):
    client = object()
    monkeypatch.setattr(api, "openrouter_client", lambda key: client)
    monkeypatch.setattr(api, "Cascade", Mock())
    screener = Mock()
    monkeypatch.setattr(api, "ModelScreener", screener)
    settings = Settings(openrouter_api_key="test", screen_model=model, screen_timeout_seconds=75.0)
    api.build_cascade(settings, [])
    primary, recall, confirmation = screener.call_args_list
    assert primary.args == (client, model, settings.screen_effort)
    assert primary.kwargs == {
        "provider_order": DEFAULT_PROVIDERS.get(model, ()),
        "timeout_seconds": 75.0,
    }
    assert recall.kwargs == {
        "max_completion_tokens": settings.rescreen_max_completion_tokens,
        "provider_order": DEFAULT_PROVIDERS[settings.rescreen_model],
        "operation": "rescreen",
    }
    assert confirmation.kwargs == {
        "provider_order": DEFAULT_PROVIDERS[settings.confirm_model],
        "operation": "confirm",
    }
    digester = api.build_digester(settings)
    assert digester.provider_order == DEFAULT_PROVIDERS[settings.digest_model]
