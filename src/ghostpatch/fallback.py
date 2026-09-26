"""Keep working when a free quota runs out: switch to the next provider mid-conversation.

`FallbackClient` looks like an OpenAI client to the agent. When the current provider says its
quota is used up (or can't be reached, or rejects the key), it moves on to the next model in
the chain and retries the same request, so the agent never notices except for a message.
Short per-minute rate limits are *not* treated as exhaustion: the agent waits those out.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Callable

from ghostpatch.providers import ModelConfig, describe_api_error, is_out_of_quota

# Gemini 3 insists that function calls in the history carry a "thought signature". Calls made
# by another provider have none; Google documents this placeholder for exactly that case.
GEMINI_PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"


def is_exhausted(error: Exception) -> bool:
    """True when retrying the same provider soon is pointless."""
    import openai

    if isinstance(error, (openai.AuthenticationError, openai.APIConnectionError, openai.NotFoundError)):
        return True
    if isinstance(error, openai.RateLimitError):
        return is_out_of_quota(str(error))
    return False


class FallbackClient:
    def __init__(self, configs: list[ModelConfig], on_switch: Callable[[ModelConfig, ModelConfig, str], None] | None = None,
                 ui: Any = None):
        if not configs:
            raise ValueError("need at least one model")
        self.configs = configs
        self.index = 0
        self.on_switch = on_switch
        self.ui = ui  # where switch notices go; run_session points this at its recorder so runs keep them
        self.used: list[str] = [self.label(configs[0])]
        self.failures: list[str] = []  # why each abandoned provider was dropped
        self._clients: dict[int, Any] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def label(config: ModelConfig) -> str:
        return f"{config.provider.name}/{config.model}"

    @property
    def current(self) -> ModelConfig:
        return self.configs[self.index]

    def _client(self) -> Any:
        if self.index not in self._clients:
            self._clients[self.index] = self.current.client()
        return self._clients[self.index]

    def _create(self, **kwargs: Any) -> Any:
        while True:
            config = self.current
            request = dict(kwargs, model=config.model)
            if config.provider.name == "gemini":
                request["messages"] = _with_gemini_signatures(kwargs.get("messages", []))
            try:
                return self._client().chat.completions.create(**request)
            except Exception as error:
                if not is_exhausted(error) or self.index + 1 >= len(self.configs):
                    raise
                self.index += 1
                self.used.append(self.label(self.current))
                reason = describe_api_error(error, config.provider)[:200]
                self.failures.append(reason)
                if self.on_switch:
                    self.on_switch(config, self.current, reason)
                if self.ui is not None:
                    self.ui.thought(f"_{reason} Switching to {self.label(self.current)} and carrying on._")


def _with_gemini_signatures(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if calls and any("extra_content" not in c for c in calls):
            message = copy.deepcopy(message)
            for call in message["tool_calls"]:
                call.setdefault("extra_content", {"google": {"thought_signature": GEMINI_PLACEHOLDER_SIGNATURE}})
        out.append(message)
    return out


def make_client(config: ModelConfig, ui: Any = None, fallback: bool = True) -> FallbackClient:
    """A client for `config` that falls back to the other configured free providers."""
    from ghostpatch.providers import fallback_chain

    return FallbackClient(fallback_chain(config) if fallback else [config], ui=ui)
