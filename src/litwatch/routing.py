"""OpenRouter provider routing: every request names the providers allowed to serve it.

Provider lists are per model, not per stage, because endpoints are model-specific: an
overridden model never inherits another model's list. A listed model is served only by its
providers, tried in order. Every request sets `require_parameters`, so an endpoint that does
not support the schema or reasoning parameters is skipped. That check alone did not keep out
endpoints that accept `reasoning` and return no reasoning tokens, which is why the lists exist.
"""

# Chosen from the inference journal and a provider trial; see evals/2026-10-01-providers.md.
DEFAULT_PROVIDERS: dict[str, tuple[str, ...]] = {
    "openai/gpt-5.6-luna": ("openai/flex", "openai"),
    "openai/gpt-6-luna": ("openai/flex", "openai"),
    # Cheapest endpoints that reasoned and agreed with the others; Wafer reasons longest.
    "z-ai/glm-5.3": ("baidu", "reka", "wafer"),
    "meta/muse-spark-1.3": ("meta",),
    "meta/muse-spark-1.3-contributor": ("meta",),
    # Structured outputs, no third-party fp4 quantization; the official endpoint is the fallback.
    "moonshotai/kimi-k3": ("wafer", "moonshotai", "fireworks"),
}


def provider_preferences(providers: tuple[str, ...]) -> dict:
    """The request's `provider` object. No providers leaves the choice to OpenRouter."""
    preferences: dict = {"require_parameters": True}
    if providers:
        preferences |= {"order": list(providers), "allow_fallbacks": False}
    return preferences


def parse_providers(raw: str, defaults: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """`model=a|b;model2=c` over the defaults; `model=` hands that model to OpenRouter."""
    merged = dict(defaults)
    for entry in raw.split(";"):
        if "=" not in entry:
            continue
        model, providers = entry.split("=", 1)
        merged[model.strip()] = tuple(p.strip() for p in providers.split("|") if p.strip())
    return merged
