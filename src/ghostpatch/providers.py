"""Model providers. All of them speak the OpenAI chat-completions API, so one client works for all."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str | None
    key_env: str | None  # environment variable holding the API key; None = no key needed
    default_model: str
    signup_url: str
    free: bool


PROVIDERS = {
    "gemini": Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env="GEMINI_API_KEY",
        default_model="gemini-3.8-flash",
        signup_url="https://aistudio.google.com/apikey",
        free=True,
    ),
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="qwen/qwen3.8-27b",
        signup_url="https://console.groq.com/keys",
        free=True,
    ),
    "ollama": Provider(
        name="ollama",
        base_url="http://localhost:11434/v1",
        key_env=None,
        default_model="qwen2.5-coder:7b",
        signup_url="https://ollama.com/download",
        free=True,
    ),
    "openai": Provider(
        name="openai",
        base_url=None,
        key_env="OPENAI_API_KEY",
        default_model="gpt-5-mini",
        signup_url="https://platform.openai.com/api-keys",
        free=False,
    ),
}
DEFAULT_PROVIDER = "gemini"


def api_key_for(provider: Provider) -> str | None:
    if provider.key_env is None:
        return "ollama"  # the OpenAI client requires some key; Ollama ignores it
    return os.environ.get(provider.key_env)


class ProviderError(Exception):
    """A setup problem the user can fix (unknown provider, missing key)."""


@dataclass(frozen=True)
class ModelConfig:
    provider: Provider
    model: str
    api_key: str

    def client(self):
        import openai  # imported lazily so `ghostpatch --help` stays fast

        return openai.OpenAI(api_key=self.api_key, base_url=self.provider.base_url)


def describe_api_error(error: Exception, provider: Provider) -> str:
    """A friendly one-line explanation of an error returned by the model provider."""
    import openai

    if isinstance(error, openai.AuthenticationError):
        return f"{provider.name} rejected the API key. Check {provider.key_env}."
    if isinstance(error, openai.RateLimitError):
        return f"Rate limit or quota exceeded ({provider.name}): {error}"
    if isinstance(error, openai.APIConnectionError):
        hint = " Is Ollama running? Start it with `ollama serve`." if provider.name == "ollama" else ""
        return f"Could not connect to {provider.name}.{hint}"
    return f"{provider.name} API error: {error}"


def resolve(provider_name: str | None = None, model: str | None = None) -> ModelConfig:
    """Pick the provider, model and key from arguments, then environment variables, then defaults."""
    name = provider_name or os.environ.get("GHOSTPATCH_PROVIDER") or DEFAULT_PROVIDER
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{name}'. Choose from: {', '.join(sorted(PROVIDERS))}")
    api_key = api_key_for(provider)
    if not api_key:
        raise ProviderError(
            f"{provider.key_env} is not set.\n"
            f"Get a {'free ' if provider.free else ''}key at {provider.signup_url}\n"
            f"then add {provider.key_env}=... to your .env file (see .env.example)."
        )
    return ModelConfig(provider, model or os.environ.get("GHOSTPATCH_MODEL") or provider.default_model, api_key)
