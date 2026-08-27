"""Auxiliary LLM client — stateless single-shot completions for tool use.

Why a separate layer instead of calling agent.chat()?

1. No tool loop — auxiliary calls are pure completions, never trigger tool calls
   (avoids recursive agent→tool→agent→tool loops)
2. No state pollution — results don't write to session history
3. Model flexibility — different tasks can use different models/providers
4. No system prompt overhead — no full agent prompt + tool schemas injected
5. Independent timeouts — per-task timeout instead of agent global timeout

Config (config.yaml):
    auxiliary:
      compression:
        model: glm-4-flash        # cheaper model for compression
      vision:
        model: gpt-4o             # vision-capable model
      title:
        model: glm-4-flash        # cheap model for titles
        timeout: 10
"""

import logging
import os
import re
from typing import Any, Optional

from openai import OpenAI
import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = {
    "compression": "",   # fall back to main model
    "vision": "",       # needs vision-capable model
    "title": "",        # cheap model is fine
    "image_gen": "dall-e-3",
    "tts": "tts-1",
}

_DEFAULT_TIMEOUTS = {
    "compression": 30,
    "vision": 60,
    "title": 10,
    "approval_judge": 10,
    "image_gen": 60,
    "tts": 30,
}


def extract_content_or_reasoning(response) -> str:
    """Extract content from an LLM response, falling back to reasoning fields.

    Reasoning models (DeepSeek-R1, Qwen-QwQ, etc.) may return content=None
    with reasoning in structured fields.

    Resolution order:
      1. message.content — strip inline think/reasoning blocks
      2. message.reasoning / message.reasoning_content
      3. message.reasoning_details — array format (OpenRouter)
    """
    msg = response.choices[0].message
    content = (msg.content or "").strip()

    if content:
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|REASONING_SCRATCHPAD)>"
            r".*?"
            r"</(?:think|thinking|reasoning|REASONING_SCRATCHPAD)>",
            "", content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned

    reasoning_parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        val = getattr(msg, field, None)
        if val and isinstance(val, str) and val.strip() and val not in reasoning_parts:
            reasoning_parts.append(val.strip())

    details = getattr(msg, "reasoning_details", None)
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = (
                    detail.get("summary")
                    or detail.get("content")
                    or detail.get("text")
                )
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(summary.strip() if isinstance(summary, str) else str(summary))

    if reasoning_parts:
        return "\n\n".join(reasoning_parts)

    return ""


class AuxiliaryClient:
    """Stateless LLM client for auxiliary (non-agent-loop) completions.

    Initialized once with the main model credentials, then each call can
    optionally override model/timeout per task.
    """

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 config: dict = None):
        self._base_url = base_url
        self._api_key = api_key
        self._default_model = model
        self._config = config or {}
        self._client = OpenAI(
            api_key=api_key, base_url=base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) if base_url and api_key else None

    _TASK_CONFIG_KEYS = {
        "vision": "vision_model",
    }

    def _resolve(self, task: str, model: str = None, timeout: float = None) -> tuple[str, float]:
        """Resolve effective model and timeout for a task.

        Priority: explicit arg > top-level config key > auxiliary section > default.

        When a top-level config key (e.g. vision_model) is explicitly set to empty
        string, the task is considered **disabled** — no fallback to default model.
        """
        top_key = self._TASK_CONFIG_KEYS.get(task)
        top_val = self._config.get(top_key) if top_key else None

        if top_key and top_key in self._config and top_val == "":
            effective_timeout = timeout or _DEFAULT_TIMEOUTS.get(task, 30)
            return "", effective_timeout

        aux_cfg = self._config.get("auxiliary", {}) or {}
        task_cfg = aux_cfg.get(task, {}) or {}

        effective_model = (
            model
            or (top_val if top_val else None)
            or task_cfg.get("model")
            or self._default_model
        )

        effective_timeout = timeout or task_cfg.get("timeout") or _DEFAULT_TIMEOUTS.get(task, 30)

        return effective_model, effective_timeout

    @property
    def client(self) -> Optional[OpenAI]:
        return self._client

    def _build_call_kwargs(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 4000,
        temperature: float = None,
        timeout: float = 30.0,
    ) -> dict:
        """Build kwargs for chat.completions.create()."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return kwargs

    def call_llm(
        self,
        task: str,
        messages: list[dict],
        model: str = None,
        max_tokens: int = 4000,
        temperature: float = None,
        timeout: float = None,
    ) -> Optional[Any]:
        """Single-shot LLM completion. Returns the raw response object.

        Args:
            task: Task name for model/timeout resolution (e.g., "compression", "title").
            messages: Chat messages list.
            model: Explicit model override.
            max_tokens: Max output tokens.
            temperature: Sampling temperature.
            timeout: Request timeout in seconds.

        Returns:
            Response object with .choices[0].message.content, or None on failure.
        """
        if not self._client:
            logger.debug("AuxiliaryClient not configured (no base_url/api_key)")
            return None

        effective_model, effective_timeout = self._resolve(task, model, timeout)

        if not effective_model:
            logger.debug("Auxiliary %s: no model resolved (task disabled)", task)
            return None

        logger.info("Auxiliary %s: using %s", task, effective_model)

        kwargs = self._build_call_kwargs(
            effective_model, messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=effective_timeout,
        )

        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            # Retry with max_completion_tokens if max_tokens is rejected
            if "max_tokens" in err_str.lower() or "unsupported_parameter" in err_str.lower():
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max_tokens
                try:
                    return self._client.chat.completions.create(**kwargs)
                except Exception:
                    pass
            logger.warning("Auxiliary call_llm(task=%s, model=%s) failed: %s", task, effective_model, e)
            return None

    def call_vision(
        self,
        messages: list[dict],
        model: str = None,
        max_tokens: int = 2000,
        timeout: float = None,
    ) -> Optional[Any]:
        """Vision LLM completion. Same as call_llm but with vision defaults."""
        return self.call_llm(
            task="vision",
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def generate_image(
        self,
        prompt: str,
        model: str = None,
        size: str = "1024x1024",
        n: int = 1,
        style: str = "vivid",
        timeout: float = None,
    ) -> Optional[Any]:
        """Generate images via DALL-E style API."""
        if not self._client:
            return None

        effective_model, effective_timeout = self._resolve("image_gen", model, timeout)

        try:
            return self._client.images.generate(
                model=effective_model or "dall-e-3",
                prompt=prompt,
                size=size,
                n=n,
                style=style,
                timeout=effective_timeout,
            )
        except Exception as e:
            logger.warning("Auxiliary generate_image failed: %s", e)
            return None

    def text_to_speech(
        self,
        text: str,
        voice: str = "alloy",
        model: str = None,
        timeout: float = None,
    ) -> Optional[Any]:
        """Generate speech via TTS API."""
        if not self._client:
            return None

        effective_model, effective_timeout = self._resolve("tts", model, timeout)

        try:
            return self._client.audio.speech.create(
                model=effective_model or "tts-1",
                voice=voice,
                input=text,
                timeout=effective_timeout,
            )
        except Exception as e:
            logger.warning("Auxiliary text_to_speech failed: %s", e)
            return None

    def is_available(self, task: str = None) -> bool:
        """Check if the client is configured and (optionally) a task is ready."""
        if not self._client:
            return False
        if task == "vision":
            model, _ = self._resolve("vision")
            return bool(model)
        if task == "image_gen":
            enabled = ((self._config.get("auxiliary") or {}).get("image_gen") or {}).get("enabled")
            return str(enabled).lower() in ("1", "true", "yes")
        if task == "tts":
            enabled = ((self._config.get("auxiliary") or {}).get("tts") or {}).get("enabled")
            return str(enabled).lower() in ("1", "true", "yes")
        return True
