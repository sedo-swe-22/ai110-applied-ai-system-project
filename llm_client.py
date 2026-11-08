"""LLM adapter for the PawPal+ agent.

The agent loop only talks to the small `LLMClient` interface below, using
provider-neutral messages. `GeminiClient` is the real implementation (Google
`google-genai` SDK, manual function calling); tests use a scripted fake client
instead, so pytest never needs an API key or spends free-tier quota.

Neutral history format (a list of dicts):
    {"role": "user",  "text": "..."}
    {"role": "model", "text": "...", "tool_calls": [ToolCall, ...], "raw": <provider content>}
    {"role": "tool",  "results": [(ToolCall, result_dict), ...]}
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from logging_config import log_event, null_logger

# Model notes (free-tier key, checked 2026-11-06/07):
# - gemini-2.5-flash: 404 "no longer available to new users".
# - gemini-3.8-flash: repeated 504/503 overload errors.
# - gemini-3.5-flash: works, but the free daily quota ran out after ~25 calls (a few chats).
# - gemini-3.5-flash-lite: handled add + clash repair, reschedule and clarifying questions
#   correctly, with a much larger free daily quota. PawPal+ doesn't need a bigger model.
# Override with PAWPAL_MODEL.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_THINKING = "LOW"  # MINIMAL / LOW / MEDIUM / HIGH, or DEFAULT for the model's own choice


class LLMError(RuntimeError):
    """The model could not be reached or refused the request (after retries)."""


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)
    id: Optional[str] = None


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-specific content, sent back unchanged next turn


MAX_SERVER_DELAY_S = 60.0


def _error_details(exc: Exception) -> list[dict]:
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return []
    items = (details.get("error") or {}).get("details") or []
    return [item for item in items if isinstance(item, dict)]


def _server_retry_delay(exc: Exception) -> Optional[float]:
    """Read Gemini's suggested wait (RetryInfo.retryDelay like "37s"), capped at 60s."""
    for item in _error_details(exc):
        if str(item.get("@type", "")).endswith("RetryInfo"):
            text = str(item.get("retryDelay", "")).rstrip("s")
            try:
                return min(float(text), MAX_SERVER_DELAY_S)
            except ValueError:
                return None
    return None


def quota_violation(exc: Exception) -> Optional[dict]:
    """Return the quota a 429 hit, e.g. {"quota_id": "GenerateRequestsPerDay...", "limit": "20"}."""
    for item in _error_details(exc):
        if str(item.get("@type", "")).endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                return {
                    "quota_id": violation.get("quotaId"),
                    "metric": violation.get("quotaMetric"),
                    "limit": violation.get("quotaValue"),
                }
    return None


def is_daily_quota(violation: Optional[dict]) -> bool:
    """True when the per-day quota is used up; waiting a minute won't help then."""
    return bool(violation and "perday" in str(violation.get("quota_id", "")).lower())


class LLMClient(Protocol):
    model: str

    def generate(self, system_prompt: str, history: list[dict], tools: list[dict]) -> LLMResponse:
        """Send the conversation and tool definitions; return text and/or tool calls."""
        ...


class UnavailableClient:
    """Stand-in used when Gemini can't be set up (e.g. no API key).

    The agent's input guardrails run before any model call, so blocked requests
    still get their normal reply. Any request that needs the model fails with
    `reason`, and the agent turns that into a clean failed run (nothing saved).
    """

    model = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def generate(self, system_prompt: str, history: list[dict], tools: list[dict]) -> LLMResponse:
        raise LLMError(self.reason)


class GeminiClient:
    """`LLMClient` backed by the Gemini API, with retry and backoff on rate limits."""

    RETRY_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        base_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        temperature: float = 0.2,
        timeout_s: Optional[float] = None,
    ) -> None:
        from google import genai  # imported here so tests never need the SDK configured
        from google.genai import types

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.model = model or os.environ.get("PAWPAL_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s or float(os.environ.get("PAWPAL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        level = os.environ.get("PAWPAL_THINKING", DEFAULT_THINKING).strip().upper()
        self.thinking_level = None if level in {"", "DEFAULT", "OFF"} else level
        # Without a timeout a stuck request would block the app forever.
        self._client = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000))
        )
        self.logger = logger or null_logger()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.temperature = temperature
        self.run_id: Optional[str] = None  # set by the agent so retries are logged per run

    # -- conversion -----------------------------------------------------------

    @staticmethod
    def _contents(history: list[dict]) -> list:
        from google.genai import types

        contents = []
        for message in history:
            role = message["role"]
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message["text"])]))
            elif role == "model":
                if message.get("raw") is not None:
                    contents.append(message["raw"])  # keeps Gemini's thought signatures intact
                else:
                    parts = []
                    if message.get("text"):
                        parts.append(types.Part.from_text(text=message["text"]))
                    for call in message.get("tool_calls", []):
                        parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.name, args=call.args)))
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                parts = [
                    types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))
                    for call, result in message["results"]
                ]
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def _config(self, system_prompt: str, tools: list[dict]):
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]
            )
            for t in tools
        ]
        return types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
            # Scheduling calls are simple; long "thinking" caused 504 deadline errors.
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level) if self.thinking_level else None,
        )

    @staticmethod
    def _parse(response) -> LLMResponse:
        candidates = getattr(response, "candidates", None) or []
        if not candidates or candidates[0].content is None:
            feedback = getattr(response, "prompt_feedback", None)
            raise LLMError(f"Gemini returned no answer (blocked or empty). {feedback or ''}".strip())
        content = candidates[0].content
        texts, calls = [], []
        for part in content.parts or []:
            if part.function_call is not None:
                fc = part.function_call
                calls.append(ToolCall(name=fc.name, args=dict(fc.args or {}), id=fc.id))
            elif part.text and not part.thought:
                texts.append(part.text)
        return LLMResponse(text="".join(texts).strip(), tool_calls=calls, raw=content)

    # -- call with retry ------------------------------------------------------

    def generate(self, system_prompt: str, history: list[dict], tools: list[dict]) -> LLMResponse:
        from google.genai import errors

        contents = self._contents(history)
        config = self._config(system_prompt, tools)
        attempt = 0
        while True:
            try:
                response = self._client.models.generate_content(model=self.model, contents=contents, config=config)
                return self._parse(response)
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                quota = quota_violation(exc) if code == 429 else None
                if is_daily_quota(quota):
                    # Retrying can't help until the quota resets, so fail fast with a clear message.
                    log_event(self.logger, "llm_quota_exhausted", self.run_id, level=logging.ERROR,
                              model=self.model, **quota)
                    limit = f" ({quota['limit']} requests/day)" if quota.get("limit") else ""
                    raise LLMError(
                        f"The free daily Gemini quota for {self.model}{limit} is used up. It resets at "
                        "midnight Pacific time, or set PAWPAL_MODEL to another model in .env."
                    ) from exc
                if code in self.RETRY_CODES and attempt < self.max_retries:
                    delay = _server_retry_delay(exc) or self.base_delay * (2 ** attempt)
                    log_event(self.logger, "llm_retry", self.run_id, level=logging.WARNING,
                              code=code, attempt=attempt + 1, delay_s=delay, **(quota or {}))
                    self.sleep(delay)
                    attempt += 1
                    continue
                if code == 429:
                    raise LLMError("Gemini free-tier rate limit reached. Wait a minute and try again.") from exc
                raise LLMError(f"Gemini API error {code}: {getattr(exc, 'message', exc)}") from exc
            except LLMError:
                raise
            except Exception as exc:  # timeouts, network problems, SDK bugs, ...
                if "timeout" in type(exc).__name__.lower() and attempt < self.max_retries:
                    log_event(self.logger, "llm_retry", self.run_id, level=logging.WARNING,
                              code="timeout", attempt=attempt + 1, delay_s=self.base_delay)
                    self.sleep(self.base_delay)
                    attempt += 1
                    continue
                raise LLMError(f"Could not reach Gemini: {type(exc).__name__}: {exc}") from exc
