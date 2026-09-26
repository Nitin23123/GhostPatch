"""Model providers. All of them speak the OpenAI chat-completions API, so one client works for all."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str | None
    key_env: str | None  # environment variable holding the API key; None = no key needed
    default_model: str
    signup_url: str
    free: bool
    headers: tuple[tuple[str, str], ...] = ()  # extra HTTP headers sent with every request


# Dictionary order is the fallback order: the most generous free tiers come first.
PROVIDERS = {
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="qwen/qwen3.8-27b",
        signup_url="https://console.groq.com/keys",
        free=True,
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        default_model="qwen/qwen3.8-27b:free",  # the ":free" variants cost nothing
        signup_url="https://openrouter.ai/keys",
        free=True,
        headers=(("HTTP-Referer", "https://github.com/Nitin23123/GhostPatch"), ("X-Title", "GhostPatch")),
    ),
    "gemini": Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env="GEMINI_API_KEY",
        default_model="gemini-3.8-flash",
        signup_url="https://aistudio.google.com/apikey",
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
DEFAULT_PROVIDER = "groq"


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

        return openai.OpenAI(api_key=self.api_key, base_url=self.provider.base_url,
                             default_headers=dict(self.provider.headers) or None)


# Phrases providers use for each kind of limit. Groq and OpenAI spell them out, Gemini names a
# quota id, OpenRouter says "free-models-per-day".
_LIMIT_KINDS = [
    (("insufficient_quota", "credit_balance"), None),
    (("tokens per day",), "daily token"),
    (("requests per day", "PerDay", "per-day"), "daily request"),
    (("tokens per minute",), "per-minute token"),
    (("requests per minute", "PerMinute", "per-min"), "per-minute request"),
]
_OUT_OF_QUOTA = ("insufficient_quota", "credit_balance", "PerDay", "per day", "per-day")


def is_out_of_quota(text: str) -> bool:
    """True for a rate limit that waiting a minute won't fix: no credits, or a daily quota used up."""
    return any(marker in text for marker in _OUT_OF_QUOTA)


def summarize_rate_limit(text: str) -> str:
    """Turn a provider's raw 429 message into a few words: which limit was hit and when to retry."""
    kind = next((kind for needles, kind in _LIMIT_KINDS if any(n in text for n in needles)), "")
    if kind is None:
        return "out of credits"
    if kind:
        size = re.search(r"\b[Ll]imit:? (\d+)", text)  # Groq: "Limit 500000", Gemini: "limit: 20"
        summary = f"{kind} limit" + (f" of {int(size.group(1)):,}" if size else "") + " reached"
    else:
        summary = "rate limited"
    retry = re.search(r"try again in ((?:\d+h)?(?:\d+m)?\d+)(?:\.\d+)?s", text)  # Groq/OpenAI; Gemini's is unreliable
    if retry:
        summary += f"; try again in {retry.group(1)}s"
    return summary


def describe_api_error(error: Exception, provider: Provider) -> str:
    """A friendly one-line explanation of an error returned by the model provider."""
    import openai

    if isinstance(error, openai.AuthenticationError):
        return f"{provider.name} rejected the API key. Check {provider.key_env}."
    if isinstance(error, openai.RateLimitError):
        return f"{provider.name}: {summarize_rate_limit(str(error))}."
    if isinstance(error, openai.APIConnectionError):
        hint = " Is Ollama running? Start it with `ollama serve`." if provider.name == "ollama" else ""
        return f"Could not connect to {provider.name}.{hint}"
    return f"{provider.name} API error: {error}"


def resolve(provider_name: str | None = None, model: str | None = None, env_model: bool = True) -> ModelConfig:
    """Pick the provider, model and key from arguments, then environment variables, then defaults.

    `env_model=False` ignores GHOSTPATCH_MODEL, which names a model of the *main* provider.
    """
    name = provider_name or os.environ.get("GHOSTPATCH_PROVIDER") or DEFAULT_PROVIDER
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{name}'. Choose from {', '.join(PROVIDERS)}, "
                            "or run `ghostpatch init`.")
    api_key = api_key_for(provider)
    if not api_key:
        raise ProviderError(
            f"{provider.key_env} is not set.\n"
            f"Get a {'free ' if provider.free else ''}key at {provider.signup_url}\n"
            f"then run `ghostpatch init` to save it (or add {provider.key_env}=... to a .env file)."
        )
    chosen = model or (os.environ.get("GHOSTPATCH_MODEL") if env_model else None) or provider.default_model
    return ModelConfig(provider, chosen, api_key)


def fallback_chain(primary: ModelConfig) -> list[ModelConfig]:
    """The main model followed by the backups to try when its quota runs out.

    GHOSTPATCH_FALLBACK="gemini,ollama" sets the order explicitly ("none" turns fallback off).
    Otherwise every other free provider that has an API key configured is used.
    """
    setting = os.environ.get("GHOSTPATCH_FALLBACK", "").strip()
    if setting.lower() == "none":
        return [primary]
    if setting:
        names = [n.strip() for n in setting.split(",") if n.strip()]
    else:
        names = [n for n, p in PROVIDERS.items() if p.free and p.key_env and os.environ.get(p.key_env)]
    chain = [primary]
    for name in names:
        if name == primary.provider.name or any(c.provider.name == name for c in chain):
            continue
        try:
            chain.append(resolve(name, env_model=False))
        except ProviderError:
            continue  # not configured; skip it
    return chain
